// Weldment Cut List panel — ported from host/src/ui/cutlist_panel.h (C5).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace app { struct AppState; }

namespace ui {

using AppState = app::AppState;

class CutListPanel {
public:
    void draw(AppState& st);

private:
    void refresh(AppState& st);

    bool           cacheValid_ = false;
    std::string    cacheDocId_;
    int            cacheRevision_ = -1;
    nlohmann::json rows_;
    int            totalMembers_ = 0;
    double         totalLengthMm_ = 0.0;
    std::vector<std::string> profiles_;
};

}  // namespace ui
