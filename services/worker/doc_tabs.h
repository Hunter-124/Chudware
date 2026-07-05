#pragma once
// Multi-document tab model (data + helpers). Tab switch logic lives in the app binding layer.

#include <string>
#include <vector>

#include "doc_session.h"

namespace core {

enum class DocTabKind { Part, Assembly, Drawing, Schematic, Pcb };

struct DocTab {
    DocTabKind kind = DocTabKind::Part;
    std::string docId;
    std::string title;
    std::string path;
    bool dirty = false;
    bool isAssembly = false;
    bool isStarter = false;
    float yaw = 35.f, pitch = 28.f, zoom = 1.f;
    float pan[3] = {0, 0, 0};
    bool camValid = false;
    DocSessionState session;
    std::string drawingDocId;
    std::string sourceDocId;
    std::string ecadDocId;
    int ecadRevision = -1;

    // T-DP13: per-tab agent attribution + live activity state.
    // agentRunning drives the live dot/spinner in doc_tabs.cpp drawOneTab().
    // agentOwner/agentModel are set from workspace_status events (aicad.workspace/1).
    bool        agentRunning = false;   // true while an agent holds a write lease on this doc
    std::string agentOwner;             // role name, e.g. "feature_modeler"
    std::string agentModel;             // model slug, e.g. "qwen2.5-coder-7b"
    float       agentSpinT = 0.f;       // spinner animation time (seconds, per-tab)

    std::string displayTitle() const;
};

inline bool isCadTab(const DocTab& t) {
    return t.kind == DocTabKind::Part || t.kind == DocTabKind::Assembly;
}

int tabIndexForDocId(const std::vector<DocTab>& tabs, const std::string& docId);

}  // namespace core
