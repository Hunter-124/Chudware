#pragma once
#include <tuple>
#include <map>
#include <memory>
#include <functional>

namespace ui::chat_persistence {
// Exact fallback for mutations outside the event seam (late answers, render toggles).
// Revisions reject event-mutated messages before this comparison.
inline bool sameMessage(const ChatMsg& a, const ChatMsg& b, bool activity = true) {
    if (std::tie(a.fromUser, a.ts, a.text, a.imagePaths, a.running, a.compactionRun, a.steerSplit,
                 a.steerId, a.steerState, a.model, a.errKind, a.errorHuman, a.errorFull,
                 a.errFullOpen, a.liveStatus, a.curMissionTask, a.planApprovePending,
                 a.planApproveRequest, a.planApproveExtras, a.planApproveReason,
                 a.resultPartial, a.copilotMode, a.cacheReadTok, a.cacheReadTotTok,
                 a.cacheReadCalls) !=
        std::tie(b.fromUser, b.ts, b.text, b.imagePaths, b.running, b.compactionRun, b.steerSplit,
                 b.steerId, b.steerState, b.model, b.errKind, b.errorHuman, b.errorFull,
                 b.errFullOpen, b.liveStatus, b.curMissionTask, b.planApprovePending,
                 b.planApproveRequest, b.planApproveExtras, b.planApproveReason,
                 b.resultPartial, b.copilotMode, b.cacheReadTok, b.cacheReadTotTok,
                 b.cacheReadCalls)) return false;
    if (std::tie(a.plan.present, a.plan.name, a.plan.summary, a.plan.fit) !=
        std::tie(b.plan.present, b.plan.name, b.plan.summary, b.plan.fit) ||
        a.plan.parts.size() != b.plan.parts.size()) return false;
    for (size_t i = 0; i < a.plan.parts.size(); ++i) {
        const auto& x = a.plan.parts[i]; const auto& y = b.plan.parts[i];
        if (std::tie(x.name, x.purpose, x.dims, x.features) !=
            std::tie(y.name, y.purpose, y.dims, y.features)) return false;
    }
    if (std::tie(a.planState.present, a.planState.shape, a.planState.title,
                 a.planState.summary, a.planState.revision, a.planState.fromEvent) !=
        std::tie(b.planState.present, b.planState.shape, b.planState.title,
                 b.planState.summary, b.planState.revision, b.planState.fromEvent) ||
        a.planState.items.size() != b.planState.items.size() ||
        a.planState.edges.size() != b.planState.edges.size()) return false;
    for (size_t i = 0; i < a.planState.items.size(); ++i) {
        const auto& x = a.planState.items[i]; const auto& y = b.planState.items[i];
        if (std::tie(x.id, x.title, x.desc, x.status, x.kind, x.phase, x.parent_id) !=
            std::tie(y.id, y.title, y.desc, y.status, y.kind, y.phase, y.parent_id)) return false;
    }
    for (size_t i = 0; i < a.planState.edges.size(); ++i)
        if (std::tie(a.planState.edges[i].from, a.planState.edges[i].to) !=
            std::tie(b.planState.edges[i].from, b.planState.edges[i].to)) return false;
    if (a.asks.size() != b.asks.size() || a.contextRefs.size() != b.contextRefs.size()) return false;
    for (size_t i = 0; i < a.asks.size(); ++i) {
        const auto& x = a.asks[i]; const auto& y = b.asks[i];
        if (std::tie(x.batchId, x.runId, x.sessionId, x.nodeId, x.agentId, x.prompt,
                     x.status, x.error, x.answers, x.reply) !=
            std::tie(y.batchId, y.runId, y.sessionId, y.nodeId, y.agentId, y.prompt,
                     y.status, y.error, y.answers, y.reply) ||
            x.questions.size() != y.questions.size()) return false;
        for (size_t qi = 0; qi < x.questions.size(); ++qi)
            if (x.questions[qi].src != y.questions[qi].src) return false;
    }
    for (size_t i = 0; i < a.contextRefs.size(); ++i) {
        const auto& x = a.contextRefs[i]; const auto& y = b.contextRefs[i];
        if (std::tie(x.kind, x.id, x.docId, x.docName, x.docPath, x.label, x.type,
                     x.owner, x.ownerName, x.component, x.componentName, x.selector,
                     x.bodyId, x.topoId, x.revision) !=
            std::tie(y.kind, y.id, y.docId, y.docName, y.docPath, y.label, y.type,
                     y.owner, y.ownerName, y.component, y.componentName, y.selector,
                     y.bodyId, y.topoId, y.revision)) return false;
    }
    if (!activity) return true;
    if (a.activity.size() != b.activity.size()) return false;
    for (size_t i = 0; i < a.activity.size(); ++i) {
        const auto& x = a.activity[i]; const auto& y = b.activity[i];
        if (std::tie(x.stage, x.title, x.detail, x.reason, x.status, x.durationMs,
                     x.tokensIn, x.tokensOut, x.opsCount, x.expanded, x.missionHeader,
                     x.rowKind, x.ctxTokens, x.streaming, x.userToggled) !=
            std::tie(y.stage, y.title, y.detail, y.reason, y.status, y.durationMs,
                     y.tokensIn, y.tokensOut, y.opsCount, y.expanded, y.missionHeader,
                     y.rowKind, y.ctxTokens, y.streaming, y.userToggled)) return false;
    }
    return true;
}
// Sources are immutable after publication; only the ordered writer fills wire bytes.
struct MessageSnapshot {
    explicit MessageSnapshot(const ChatMsg& message) : source(message) {}
    const ChatMsg source;
    mutable std::shared_ptr<const std::string> bytes;
};
struct JsonFragment {
    std::function<nlohmann::json()> encode;
    mutable std::shared_ptr<const std::string> bytes;
};
struct SessionSnapshot {
    std::vector<std::shared_ptr<MessageSnapshot>> messages;
    uint64_t dagRevision = ~uint64_t(0);
    std::shared_ptr<JsonFragment> dag;
};
}  // namespace ui::chat_persistence
