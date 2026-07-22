// Parity test for the copilot mode tool-gating contract
// (contracts/copilot_modes.json + .md). Asserts the C++ loader/semantics in
// core/agent/copilot_modes.cpp produce the SAME allow/deny verdicts the Python
// path does — services/agent/test_copilot_modes_parity.py encodes the identical
// matrix. Any change to the contract data or rules must keep both tests green
// in the same commit.
//
// Run from the repo root (add_test WORKING_DIRECTORY) so the repo-relative
// contracts/copilot_modes.json fallback resolves.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/copilot_modes.h"

#include <algorithm>
#include <vector>

using aicad::agent::CopilotModes;
using aicad::agent::copilot_apply_mode_tools;
using aicad::agent::copilot_normalize_mode;
using aicad::agent::copilot_tool_allowed;

namespace {

bool allowed(const CopilotModes& cm, const char* tool, const char* mode) {
    return copilot_tool_allowed(cm, tool, mode);
}

}  // namespace

TEST_CASE("contract file loads and is self-consistent") {
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    CHECK(cm->default_mode == "agent");
    CHECK(cm->modes == std::set<std::string>({"agent", "ask", "edit", "plan", "swarm"}));
    CHECK(cm->unrestricted == std::set<std::string>({"agent", "swarm"}));
    // Shared floor is subtractable/unionable data: it must intersect every
    // restricted mode's final output (asserted per-mode below).
    CHECK(cm->shared_defaults.count("new_part"));
    CHECK(cm->shared_defaults.count("enable_tools"));
    CHECK(cm->shared_defaults.count("list_tool_categories"));
}

TEST_CASE("normalize_mode mirrors Python (absent/unknown -> agent)") {
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    CHECK(copilot_normalize_mode(*cm, "") == "agent");
    CHECK(copilot_normalize_mode(*cm, " PLAN ") == "plan");
    CHECK(copilot_normalize_mode(*cm, "bogus") == "agent");
    CHECK(copilot_normalize_mode(*cm, "ASK") == "ask");
}

// THE parity matrix: same rows/columns/verdicts as
// services/agent/test_copilot_modes_parity.py. Column order: agent plan swarm edit ask.
TEST_CASE("mode x tool verdict matrix (no role card)") {
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    struct Row {
        const char* tool;
        bool agent, plan, swarm, edit, ask;
    };
    const std::vector<Row> rows = {
        // tool                  agent plan swarm edit  ask
        {"describe_part",         true, true, true, true, true},
        {"measure_part",          true, true, true, true, true},
        {"research_web_search",   true, true, true, true, true},
        {"ask_user",              true, true, true, true, true},
        {"enable_tools",          true, true, true, true, true},
        {"list_tool_categories",  true, true, true, true, true},
        {"new_part",              true, true, true, true, true},  // shared floor
        {"save_part",             true, true, true, true, true},
        {"add_features",          true, false, true, true, false},
        {"edit_feature",          true, false, true, true, false},
        {"new_assembly",          true, false, true, false, false},
        {"cam_generate",          true, false, true, false, false},
        {"delegate",              true, true, true, true, false},
        {"corporate_activate",    true, false, true, false, false},
        {"run_fea_static",        true, true, true, false, false},
        {"inspect_part",          true, true, true, true, false},
        {"dag_run",               true, false, true, false, false},
    };
    for (const auto& r : rows) {
        CAPTURE(r.tool);
        CHECK(allowed(*cm, r.tool, "agent") == r.agent);
        CHECK(allowed(*cm, r.tool, "plan") == r.plan);
        CHECK(allowed(*cm, r.tool, "swarm") == r.swarm);
        CHECK(allowed(*cm, r.tool, "edit") == r.edit);
        CHECK(allowed(*cm, r.tool, "ask") == r.ask);
    }
}

TEST_CASE("empty tool name is denied even in unrestricted modes") {
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    CHECK_FALSE(copilot_tool_allowed(*cm, "", "agent"));
    CHECK_FALSE(copilot_tool_allowed(*cm, "   ", "swarm"));
}

TEST_CASE("agent/swarm unrestricted without a role card; role card applies") {
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    CHECK_FALSE(copilot_apply_mode_tools(*cm, "agent").has_value());
    CHECK_FALSE(copilot_apply_mode_tools(*cm, "swarm").has_value());
    // Agent keeps the role set intact (sorted).
    auto agent_role = copilot_apply_mode_tools(*cm, "agent", {"add_features", "new_part", "delegate"});
    REQUIRE(agent_role.has_value());
    CHECK(*agent_role == std::vector<std::string>({"add_features", "delegate", "new_part"}));
    // Swarm unions the corporate/delegate extras onto the role set.
    auto swarm_role = copilot_apply_mode_tools(*cm, "swarm", {"add_features"});
    REQUIRE(swarm_role.has_value());
    CHECK(std::find(swarm_role->begin(), swarm_role->end(), "add_features") != swarm_role->end());
    CHECK(std::find(swarm_role->begin(), swarm_role->end(), "delegate") != swarm_role->end());
    CHECK(std::find(swarm_role->begin(), swarm_role->end(), "corporate_activate") != swarm_role->end());
}

TEST_CASE("plan strips mutators from a role card; shared floor re-adds doc lifecycle") {
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    auto out = copilot_apply_mode_tools(
        *cm, "plan",
        {"describe_part", "add_features", "run_fea_static", "delegate", "new_part",
         "research_web_search", "edit_feature"});
    REQUIRE(out.has_value());
    auto has = [&](const char* t) { return std::find(out->begin(), out->end(), t) != out->end(); };
    CHECK(has("describe_part"));
    CHECK(has("run_fea_static"));
    CHECK(has("delegate"));
    CHECK(has("research_web_search"));
    CHECK_FALSE(has("add_features"));
    CHECK_FALSE(has("edit_feature"));
    CHECK(has("new_part"));      // shared floor restores it
    CHECK(has("enable_tools"));  // meta floor
}

TEST_CASE("ask intersects a role card, strips mutators, keeps the shared floor") {
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    auto out = copilot_apply_mode_tools(
        *cm, "ask", {"describe_part", "add_features", "new_part", "measure_part"});
    REQUIRE(out.has_value());
    auto has = [&](const char* t) { return std::find(out->begin(), out->end(), t) != out->end(); };
    CHECK(has("describe_part"));
    CHECK(has("measure_part"));
    CHECK_FALSE(has("add_features"));
    CHECK(has("new_part"));  // shared floor
}

TEST_CASE("plan_only forces plan semantics under agent mode") {
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    auto out = copilot_apply_mode_tools(
        *cm, "agent",
        {"add_features", "describe_part", "buildgraph_plan", "new_part", "run_fea_static",
         "delegate"},
        /*plan_only=*/true);
    REQUIRE(out.has_value());
    auto has = [&](const char* t) { return std::find(out->begin(), out->end(), t) != out->end(); };
    CHECK(has("buildgraph_plan"));
    CHECK(has("describe_part"));
    CHECK(has("run_fea_static"));
    CHECK(has("delegate"));
    CHECK_FALSE(has("add_features"));
    CHECK(has("new_part"));  // shared floor
}

TEST_CASE("the run.cpp delegate gate verdicts match the contract") {
    // Mirrors the handle_delegate mode gate in core/agent/run.cpp.
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    CHECK(copilot_tool_allowed(*cm, "delegate", ""));        // legacy empty mode -> agent
    CHECK(copilot_tool_allowed(*cm, "delegate", "agent"));
    CHECK(copilot_tool_allowed(*cm, "delegate", "plan"));
    CHECK(copilot_tool_allowed(*cm, "delegate", "edit"));
    CHECK(copilot_tool_allowed(*cm, "delegate", "swarm"));
    CHECK_FALSE(copilot_tool_allowed(*cm, "delegate", "ask"));
}
