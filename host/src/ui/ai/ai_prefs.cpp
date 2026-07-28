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

// ---- chat transcript display -----------------------------------------------------------
// Unlike the knobs above (read a handful of times per frame from ONE view), these are read
// once per activity row per frame — a transcript with 60 rows would do 60 config.json opens
// every frame through loadAgentInt. Cache the value on first read and let the setter be the
// single invalidation point; nothing else writes these keys.
namespace {
struct CachedInt {
    const char* key;
    int def, lo, hi;
    int  value = 0;
    bool loaded = false;

    int get() {
        if (!loaded) { value = loadAgentInt(key, def, lo, hi); loaded = true; }
        return value;
    }
    void set(int v) {
        if (v < lo) v = lo;
        if (v > hi) v = hi;
        value = v;
        loaded = true;
        saveAgentInt(key, v);
    }
};

CachedInt g_toolDetail   {"ui_chat_tool_detail",   1, 0, 2};
CachedInt g_collapseAnim {"ui_chat_collapse_anim", 1, 0, 1};
CachedInt g_groupCalls   {"ui_chat_group_calls",   1, 0, 1};
CachedInt g_showRenders  {"ui_chat_show_renders",  1, 0, 1};
CachedInt g_humanizeIds  {"ui_chat_humanize_ids",  1, 0, 1};
}  // namespace

int  chatToolDetail()                { return g_toolDetail.get(); }
void setChatToolDetail(int v)        { g_toolDetail.set(v); }

bool chatCollapseAnim()              { return g_collapseAnim.get() != 0; }
void setChatCollapseAnim(bool on)    { g_collapseAnim.set(on ? 1 : 0); }

bool chatGroupToolCalls()            { return g_groupCalls.get() != 0; }
void setChatGroupToolCalls(bool on)  { g_groupCalls.set(on ? 1 : 0); }

bool chatShowRenders()               { return g_showRenders.get() != 0; }
void setChatShowRenders(bool on)     { g_showRenders.set(on ? 1 : 0); }

bool chatHumanizeDocIds()            { return g_humanizeIds.get() != 0; }
void setChatHumanizeDocIds(bool on)  { g_humanizeIds.set(on ? 1 : 0); }

}  // namespace ui
