// See ai_prefs.h. Mirrors the loadMaxSubagentDepth/saveMaxSubagentDepth config.json recipe
// (copilot/settings.cpp): read agent.<key> with a clamped default; save through
// updateConfigJson (serialized, locked read-modify-write of the shared config.json).
#include "ai_prefs.h"

#include <fstream>

#include <nlohmann/json.hpp>

#include "core/app_state.h"   // configPath()

namespace ui {
namespace {

int loadAgentInt(const char* key, int def, int lo, int hi) {
    int v = def;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object() && agent.contains(key) && agent[key].is_number_integer())
                    v = agent[key].get<int>();
                else if (agent.is_object() && agent.contains(key) && agent[key].is_boolean())
                    v = agent[key].get<bool>() ? 1 : 0;
            }
        }
    } catch (...) {}
    if (v < lo) v = lo;
    if (v > hi) v = hi;
    return v;
}

void saveAgentInt(const char* key, int v) {
    updateConfigJson([&](nlohmann::json& j) {
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"][key] = v;
    });
}

}  // namespace

int  aiUiVerbosity()                 { return loadAgentInt("ui_verbosity", 2, 0, 2); }
void setAiUiVerbosity(int v)         { if (v < 0) v = 0; if (v > 2) v = 2; saveAgentInt("ui_verbosity", v); }

bool aiFollowActiveSession()         { return loadAgentInt("ui_follow_session", 1, 0, 1) != 0; }
void setAiFollowActiveSession(bool on) { saveAgentInt("ui_follow_session", on ? 1 : 0); }

bool aiActiveWorkPreviews()          { return loadAgentInt("ui_active_work_previews", 1, 0, 1) != 0; }
void setAiActiveWorkPreviews(bool on){ saveAgentInt("ui_active_work_previews", on ? 1 : 0); }

}  // namespace ui
