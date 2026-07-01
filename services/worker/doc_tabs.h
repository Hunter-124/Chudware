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

    std::string displayTitle() const;
};

inline bool isCadTab(const DocTab& t) {
    return t.kind == DocTabKind::Part || t.kind == DocTabKind::Assembly;
}

int tabIndexForDocId(const std::vector<DocTab>& tabs, const std::string& docId);

}  // namespace core
