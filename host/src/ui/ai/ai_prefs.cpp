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

// Every pref here is read from per-frame draw code (the workspace views, the Settings modal and,
// for the chat-display knobs, once per activity row), so each goes through a cached accessor:
// one config.json read per process, with the setter as the single invalidation point. Nothing
// else writes these keys, and because both AI > Options and Settings > AI read the same cache,
// a change made in one surface shows in the other on the next frame.
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
        if (loaded && v == value) return;
        value = v;
        loaded = true;
        saveAgentInt(key, v);
    }
};

CachedInt g_verbosity    {"ui_verbosity",            2, 0, 2};
CachedInt g_follow       {"ui_follow_session",       1, 0, 1};
CachedInt g_previews     {"ui_active_work_previews", 1, 0, 1};
CachedInt g_aiView       {"ui_ai_view",              0, 0, 6};
CachedInt g_hubByParent  {"ui_subagent_hub_by_parent",    0, 0, 1};
CachedInt g_hubSession   {"ui_subagent_hub_session_only", 0, 0, 1};
CachedInt g_toolDetail   {"ui_chat_tool_detail",   1, 0, 2};
CachedInt g_collapseAnim {"ui_chat_collapse_anim", 1, 0, 1};
CachedInt g_groupCalls   {"ui_chat_group_calls",   1, 0, 1};
CachedInt g_showRenders  {"ui_chat_show_renders",  1, 0, 1};
CachedInt g_humanizeIds  {"ui_chat_humanize_ids",  1, 0, 1};

}  // namespace

int  aiUiVerbosity()                   { return g_verbosity.get(); }
void setAiUiVerbosity(int v)           { g_verbosity.set(v); }

bool aiFollowActiveSession()           { return g_follow.get() != 0; }
void setAiFollowActiveSession(bool on) { g_follow.set(on ? 1 : 0); }

bool aiActiveWorkPreviews()            { return g_previews.get() != 0; }
void setAiActiveWorkPreviews(bool on)  { g_previews.set(on ? 1 : 0); }

int  aiWorkspaceView()                 { return g_aiView.get(); }
void setAiWorkspaceView(int v)         { g_aiView.set(v); }

bool aiSubagentHubByParent()            { return g_hubByParent.get() != 0; }
void setAiSubagentHubByParent(bool on)  { g_hubByParent.set(on ? 1 : 0); }

bool aiSubagentHubSessionOnly()           { return g_hubSession.get() != 0; }
void setAiSubagentHubSessionOnly(bool on) { g_hubSession.set(on ? 1 : 0); }

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
