#pragma once
// Framework-agnostic view-model for the T-U1 scaffold demo panel. Exists to exercise (and prove
// out, for T-U2..T-U8 authors to copy) the panel_scaffold.h convention checked by
// scripts/ui_style_check.py: a scaffold-built panel's per-frame state lives here, in a header
// under ui/abstract/ with no ImGui/GL includes (see ui/abstract/artifact_view.h, the T-F16
// exemplar this mirrors) — the ui/imgui/ backend renders from it, never the other way around.

#include <string>
#include <vector>

namespace aicad::ui {

struct ScaffoldDemoRow {
    std::string id;
    std::string label;
    std::string detail;
};

struct ScaffoldDemoModel {
    std::vector<ScaffoldDemoRow> rows;
};

// Case-insensitive substring filter over label+detail — the search-row behavior every scaffold
// panel is expected to implement the same way (pattern library §2).
inline std::vector<ScaffoldDemoRow> filterScaffoldDemoRows(const ScaffoldDemoModel& model,
                                                            const std::string& query) {
    if (query.empty()) return model.rows;
    std::string q = query;
    for (char& c : q) c = static_cast<char>(::tolower(static_cast<unsigned char>(c)));
    std::vector<ScaffoldDemoRow> out;
    for (const auto& row : model.rows) {
        std::string hay = row.label + " " + row.detail;
        for (char& c : hay) c = static_cast<char>(::tolower(static_cast<unsigned char>(c)));
        if (hay.find(q) != std::string::npos) out.push_back(row);
    }
    return out;
}

}  // namespace aicad::ui
