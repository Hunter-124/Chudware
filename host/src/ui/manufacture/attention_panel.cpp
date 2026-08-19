#include "attention_panel.h"

#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>

#include "imgui.h"

#include "../metrics.h"
#include "../widgets/kit.h"
#include "../../ui_theme.h"

namespace ui {
namespace {

namespace M = ui::metrics;
using kit::Tone;

// Wrap width for the one hover explanation this panel paints itself (the card title's full
// reason). Mirrors kit.cpp's own tooltip painter, which is TU-private; font-relative, so it
// tracks the UI scale instead of pinning a pixel width.
constexpr float kTooltipWrapEm = 26.0f;

// Severity -> the kit's semantic tone. An unclassified item is informational, not an alarm:
// inventing an amber for "we don't know" is exactly the noise this pass removes.
Tone severityTone(const std::string& severity) {
    if (severity == "critical") return Tone::Err;
    if (severity == "warning")  return Tone::Warn;
    return Tone::Neutral;
}

// Card wash for the severity — the palette's own status role, or no wash at all for an
// unclassified item (kit's neutral glass is the default container).
const float* severityTint(const std::string& severity) {
    if (severity == "critical") return palette.statusErr;
    if (severity == "warning")  return palette.statusWarn;
    return nullptr;
}

// kit::chip's own geometry (kit.cpp chipGeom), so cardHeaderRight reserves exactly the pill the
// chip paints and the title can be elided against what is left. In this ~360 dp rail the reason
// is almost always wider than the card, so eliding it OURSELVES is what keeps the severity chip
// on screen: cardHeaderRight refuses to rewind past the title's end.
float chipWidth(const char* label, bool dot) {
    const float padX = M::U(1.5f);
    const float gap  = M::U(1.0f);
    const float dotR = dot ? M::U(0.75f) : 0.0f;
    return padX * 2.0f + ImGui::CalcTextSize(label ? label : "").x +
           (dot ? (dotR * 2.0f + gap) : 0.0f);
}

std::string seqText(std::uint64_t seq) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%llu", static_cast<unsigned long long>(seq));
    return std::string(buf);
}

void hoverFullText(const char* full, const std::string& shown) {
    if (!full || !full[0] || shown == full) return;
    if (!ImGui::IsItemHovered()) return;
    if (!ImGui::BeginTooltip()) return;
    ImGui::PushTextWrapPos(ImGui::GetFontSize() * kTooltipWrapEm);
    ImGui::TextUnformatted(full);
    ImGui::PopTextWrapPos();
    ImGui::EndTooltip();
}

// A failure record that a LATER attempt on the same channel has already superseded is history,
// not a live problem. Only a still-standing failure gets the loud treatment.
bool deliveryStillFailed(const MfgAttentionRow& item, std::size_t index) {
    const MfgAttentionDelivery& record = item.deliveries[index];
    if (record.status != "failed" && record.status != "rate_limited") return false;
    for (std::size_t later = index + 1; later < item.deliveries.size(); ++later) {
        if (item.deliveries[later].channel == record.channel) return false;
    }
    return true;
}

Tone deliveryTone(const MfgAttentionRow& item, std::size_t index) {
    const MfgAttentionDelivery& record = item.deliveries[index];
    if (deliveryStillFailed(item, index)) return Tone::Err;
    if (record.status == "failed" || record.status == "rate_limited") return Tone::Warn;
    if (record.status == "delivered") return Tone::Ok;
    return Tone::Neutral;
}

// The pre-kit panel's exact sentence for a notification that did not arrive. Verbatim: an
// operator who searches the logs for this wording must still find it.
std::string deliveryFailureLine(const MfgAttentionDelivery& record) {
    std::string out = "Not delivered via ";
    out += record.channel.empty() ? "unnamed channel" : record.channel;
    out += " — ";
    out += record.status;
    if (!record.error.empty()) {
        out += ": ";
        out += record.error;
    }
    return out;
}

std::string deliveryTooltip(const MfgAttentionRow& item, std::size_t index) {
    const MfgAttentionDelivery& record = item.deliveries[index];
    const Tone                  tone   = deliveryTone(item, index);
    if (tone == Tone::Err) return deliveryFailureLine(record);
    if (tone == Tone::Warn) {
        return deliveryFailureLine(record) +
               "\nA later attempt on this channel superseded this record.";
    }
    return "Recorded at journal seq " + seqText(record.seq) + ".";
}

// One attention item as a severity-tinted glass card. Returns true when this frame issued the
// acknowledge RPC, so the caller stops drawing (same control flow as before the kit pass).
bool drawAttentionItem(const MfgAttentionRow& item, const MfgAttentionPanelInput& input,
                       std::size_t index) {
    ImGui::PushID(static_cast<int>(index));

    const char* sevLabel = item.severity.empty() ? "unknown" : item.severity.c_str();
    const Tone  sevTone  = severityTone(item.severity);
    const float headW    = chipWidth(sevLabel, /*dot=*/true);

    const char* reason = item.reason.empty() ? "Machine attention" : item.reason.c_str();
    std::string title  = kit::elide(reason, ImGui::GetContentRegionAvail().x -
                                                kit::padX() * 2.0f - headW - M::U(2.0f));
    if (title.empty()) title = kit::elide(reason, headW);   // pathological width: keep a header

    bool acknowledged = false;
    if (kit::cardBegin(item.id.empty() ? "attention_item" : item.id.c_str(), title.c_str(),
                       nullptr, nullptr, severityTint(item.severity))) {
        hoverFullText(reason, title);
        kit::cardHeaderRight(headW);
        kit::chip(sevLabel, sevTone, /*dot=*/true,
                  item.severity.empty() ? "The journal did not classify this item." : nullptr);

        if (item.occurrenceCount > 1) {
            const std::string seen =
                kit::fmtCount(static_cast<long long>(item.occurrenceCount)) + " occurrences";
            kit::chip(seen.c_str(), Tone::Neutral, /*dot=*/false,
                      "Occurrences the journal recorded for this item.");
        }

        kit::dimText(item.why.empty() ? "No reason was supplied." : item.why.c_str());
        kit::keyValueRow("Device", item.device.empty() ? "—" : item.device.c_str());
        kit::keyValueRow("Run", item.run.empty() ? "—" : item.run.c_str());
        const std::string raised = seqText(item.raisedSeq);
        kit::keyValueRow("Raised at journal seq", raised.c_str());

        // The owner filters closed items out of the list today, but this renderer is handed
        // whatever the journal projected: never drop the actor or the seq that closed an item.
        if (item.acknowledgedSeq != 0) {
            kit::chip("acknowledged", Tone::Ok, /*dot=*/true,
                      "The journal recorded an acknowledgement. The cause is not resolved.");
            kit::keyValueRow("Acknowledged by",
                             item.acknowledgedBy.empty() ? "unrecorded actor"
                                                         : item.acknowledgedBy.c_str(),
                             Tone::Ok);
            const std::string ackSeq = seqText(item.acknowledgedSeq);
            kit::keyValueRow("Acknowledged at seq", ackSeq.c_str(), Tone::Ok);
        }
        if (item.resolvedSeq != 0) {
            kit::chip("resolved", Tone::Neutral, /*dot=*/true,
                      "The journal recorded this item as resolved.");
            const std::string resSeq = seqText(item.resolvedSeq);
            kit::keyValueRow("Resolved at seq", resSeq.c_str());
        }

        for (std::size_t i = 0; i < item.deliveries.size(); ++i) {
            const MfgAttentionDelivery& record = item.deliveries[i];
            const Tone                  tone   = deliveryTone(item, i);
            const std::string           why    = deliveryTooltip(item, i);
            kit::valueChip(record.channel.empty() ? "unnamed channel" : record.channel.c_str(),
                           record.status.empty() ? "unknown" : record.status.c_str(), tone,
                           why.c_str());
            // A notification that never arrived cannot live in a tooltip.
            if (tone == Tone::Err) kit::inlineStatus(deliveryFailureLine(record).c_str(), Tone::Err);
        }

        const bool actorMissing = input.actor.empty();
        const bool sinkMissing  = input.acknowledge == nullptr;
        const bool blocked      = input.requestInFlight || actorMissing || sinkMissing;

        std::string note;
        if (actorMissing)               note = "Actor identity is required to acknowledge.";
        else if (sinkMissing)           note = "Acknowledgement service is unavailable.";
        else if (input.requestInFlight) note = "Acknowledgement request in flight.";
        else {
            note = "Records acknowledgement by ";
            note.append(input.actor.data(), input.actor.size());
            note += "; it does not resolve the cause.";
        }

        const bool clicked =
            kit::primaryButton("Acknowledge", !blocked, blocked ? note.c_str() : nullptr,
                               ImVec2(0.0f, M::camBtnH()));
        // The reason stays VISIBLE, not hover-only: the disabled button's tooltip repeats it.
        kit::dimText(note.c_str());

        if (clicked) {
            input.acknowledge(input.acknowledgeContext, item.id, input.actor);
            acknowledged = true;
        }
    }
    kit::cardEnd();

    ImGui::PopID();
    return acknowledged;
}

}  // namespace

void drawManufactureAttentionPanel(const MfgAttentionPanelInput& input) {
    // itemsCount is the guard: the owner parks a single default-constructed row behind itemsData
    // when the mirror has none, so the iteration range — never itemsData itself — decides whether
    // there is anything to draw.
    std::vector<const MfgAttentionRow*> open;
    open.reserve(input.size());
    for (const MfgAttentionRow& item : input) {
        if (item.acknowledgedSeq == 0 && item.resolvedSeq == 0) open.push_back(&item);
    }
    std::stable_sort(open.begin(), open.end(), [](const auto* left, const auto* right) {
        return left->raisedSeq > right->raisedSeq;
    });

    // The count is the number of cards below it: the list holds OPEN items, so spending the
    // header on the journal's total would contradict what the operator can see.
    kit::sectionHeader("Machine attention", "warning_badge", nullptr,
                       static_cast<int>(open.size()), /*first=*/true);
    kit::dimText("Items remain here until the journal records acknowledgement or resolution.");

    if (!input.requestError.empty()) {
        std::string failed = "Acknowledgement was not recorded: ";
        failed.append(input.requestError.data(), input.requestError.size());
        kit::inlineStatus(failed.c_str(), Tone::Err);
    }

    if (open.empty()) {
        kit::emptyState("ok_badge", "No open attention items",
                        "Machine stops, faults and failed notifications appear here as soon as "
                        "the journal raises them.");
        return;
    }

    for (std::size_t i = 0; i < open.size(); ++i) {
        if (i > 0) kit::gapRow();
        if (drawAttentionItem(*open[i], input, i)) return;
    }
}

}  // namespace ui
