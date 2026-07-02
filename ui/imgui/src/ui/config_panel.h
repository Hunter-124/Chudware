// Configurations panel — ported from host/src/ui/config_panel.h (C5).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace app { struct AppState; }

namespace ui {

using AppState = app::AppState;

class ConfigPanel {
public:
    void drawEmbedded(AppState& st);

private:
    void refresh(AppState& st);
    void drawBody(AppState& st);
    bool runConfigOp(AppState& st, const std::string& method, const nlohmann::json& params);

    bool        cacheValid_ = false;
    std::string cacheDocId_;
    int         cacheRevision_ = -1;
    std::vector<std::string> names_;
    std::string active_;
    nlohmann::json detail_;
    int         selected_ = -1;

    bool        addPromptOpen_ = false;
    char        addNameBuf_[128] = {0};
    bool        addActivate_ = true;
    bool        focusAddField_ = false;
};

}  // namespace ui
