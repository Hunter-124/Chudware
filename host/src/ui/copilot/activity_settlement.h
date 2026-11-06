#pragma once

#include "context_estimate.h"
#include <nlohmann/json.hpp>
#include <string>

namespace ui {

// A missing compaction end is never evidence of a committed compaction.
template <typename Activity>
bool settleUnfinishedCompaction(Activity& activity, const std::string& outcome) {
    if (activity.rowKind != 17 || activity.status != "running") return false;
    if (activity.stage.find("compaction:") == std::string::npos &&
        activity.title.rfind("Compacting context", 0) != 0 &&
        activity.title.rfind("Auto-compacting context", 0) != 0) return false;
    const bool cancelled = outcome == "cancelled" || outcome == "canceled";
    const bool failed = outcome == "failed" || outcome == "fail" || outcome == "error";
    activity.status = cancelled ? "cancelled" : failed ? "fail" : "stopped";
    activity.title = cancelled ? "Context compaction cancelled"
                               : failed ? "Context compaction failed"
                                        : "Context compaction interrupted";
    activity.streaming = false;
    ++activity.revision;
    return true;
}

template <typename Message>
void settleCompactionMessage(Message& message, const std::string& outcome) {
    for (auto& activity : message.activity)
        settleUnfinishedCompaction(activity, outcome);
    message.liveStatus.clear();
    if (message.compactionRun) message.running = false;
}

// A terminal receipt can arrive after Stop cosmetically marked its row cancelled.
// Apply it to the owning conversation even when that conversation is not visible.
template <typename Conversation, typename Message>
bool applyCommittedCompactionReceipt(const nlohmann::json& turn, int& visible,
                                     std::vector<Conversation>& conversations, bool isViewed,
                                     const std::string& owner, Message* message) {
    if (!turn.is_object() || turn.value("outcome", std::string()) != "committed" ||
        !turn.contains("compaction") || !turn["compaction"].is_object()) return false;
    const auto& compaction = turn["compaction"];
    int stored = -1;
    const auto after = compaction.find("tokensAfter");
    if (after != compaction.end() && after->is_number_integer() && after->get<int>() >= 0) {
        const int used = after->get<int>();
        stored = compaction.value("tokensAfterEstimated", false) ? storedContextEstimate(used)
                                                                  : storedContextMeasurement(used);
    }
    storeConversationContext(visible, conversations, isViewed, owner, stored);
    if (message) {
        for (auto& activity : message->activity) {
            if (activity.rowKind != 17 ||
                (activity.stage.find("compaction:") == std::string::npos &&
                 activity.title.rfind("Compacting context", 0) != 0 &&
                 activity.title.rfind("Context compaction", 0) != 0)) continue;
            if (activity.status != "done") {
                activity.status = "done";
                activity.title = "Context compacted";
                activity.detail = activity.title;
                activity.reason = "done";
                activity.streaming = false;
                ++activity.revision;
            }
        }
        message->liveStatus.clear();
        message->running = false;
    }
    return true;
}

} // namespace ui
