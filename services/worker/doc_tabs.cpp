#include "doc_tabs.h"

namespace core {

static std::string stemOf(const std::string& path) {
    size_t s = path.find_last_of("/\\");
    std::string f = (s == std::string::npos) ? path : path.substr(s + 1);
    size_t d = f.find_last_of('.');
    if (d != std::string::npos && d > 0) f = f.substr(0, d);
    return f;
}

std::string DocTab::displayTitle() const {
    if (!path.empty()) {
        std::string s = stemOf(path);
        if (!s.empty()) return s;
    }
    if (!title.empty()) return title;
    switch (kind) {
        case DocTabKind::Assembly: return "Assembly1";
        case DocTabKind::Drawing: return "Drawing1";
        case DocTabKind::Schematic: return "Schematic1";
        case DocTabKind::Pcb: return "Board1";
        default: return "Part1";
    }
}

int tabIndexForDocId(const std::vector<DocTab>& tabs, const std::string& docId) {
    for (int i = 0; i < (int)tabs.size(); ++i)
        if (tabs[i].docId == docId) return i;
    return -1;
}

}  // namespace core
