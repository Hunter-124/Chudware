// Evaluate dialogs (C19) — ported from host/src/ui/evaluate_dialogs.h (C5).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

class Renderer;

namespace app { struct AppState; }

namespace ui {

using AppState = app::AppState;

void setEvaluateRenderer(Renderer* renderer);

class EvaluateDialogs {
public:
    bool handleCommand(AppState& st, const std::string& cmdId);
    void draw(AppState& st);
    const std::string& openWindowKind() const { return openWindow_; }

private:
    void runEval(AppState& st, const std::string& method, const nlohmann::json& extra);
    void runFieldEval(AppState& st, const std::string& method, const nlohmann::json& extra,
                      const std::string& legendTitle, const std::string& units);

    std::string openWindow_;
    std::string title_;
    nlohmann::json result_;
    std::string errorText_;
    bool pending_ = false;
    int  gen_ = 0;

    int   massMaterial_ = 0;
    float massDensity_  = 7.85f;

    std::vector<std::string> measureLog_;
    int   measureLoggedGen_ = -1;
};

}  // namespace ui
