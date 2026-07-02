// Design Table panel — ported from host/src/ui/design_table_panel.h (C5).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace app { struct AppState; }

namespace ui {

using AppState = app::AppState;

class DesignTablePanel {
public:
    void draw(AppState& st);

private:
    struct Column {
        bool        suppress = false;
        std::string featureId;
        std::string param;
        std::string label;
        bool        angle = false;
        std::string key() const { return featureId + "." + param; }
    };
    struct Candidate {
        Column      col;
        std::string menuLabel;
    };

    void refresh(AppState& st);
    bool runConfigOp(AppState& st, const std::string& method, const nlohmann::json& params);
    const nlohmann::json* detailFor(const std::string& name) const;
    void buildCandidates(AppState& st, std::vector<Candidate>& out) const;
    void addColumn(const Column& c);

    bool        cacheValid_ = false;
    std::string cacheDocId_;
    int         cacheRevision_ = -1;
    std::vector<std::string> names_;
    std::string active_;
    nlohmann::json detail_;
    std::vector<Column> columns_;
    std::string columnsForDocId_;
    bool addOpen_ = false;
    bool        addCfgOpen_ = false;
    char        addCfgName_[128] = {0};
    bool        addCfgActivate_ = false;
    bool        focusAddCfg_ = false;
    int         selectedRow_ = -1;
};

}  // namespace ui
