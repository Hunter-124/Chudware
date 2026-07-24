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

using chudware::agent::CopilotModes;
using chudware::agent::copilot_apply_mode_tools;
using chudware::agent::copilot_normalize_mode;
using chudware::agent::copilot_tool_allowed;

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
    // T-file-floor: agent_default_tools / subagent_default_tools parity.
    CHECK(cm->agent_defaults == std::set<std::string>(
        {"fs_edit", "fs_list", "fs_write", "research_fetch_page", "run_shell"}));
    CHECK(cm->subagent_defaults == std::set<std::string>(
        {"fs_edit", "fs_glob", "fs_grep", "fs_list", "fs_read", "fs_write"}));
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
        // T-file-floor: agent_default_tools main-agent floor. No role card
        // here, so agent/swarm are unrestricted (None) regardless.
        {"fs_write",               true, false, true, false, false},
        {"fs_edit",                true, false, true, false, false},
        {"fs_list",                true, false, true, false, false},
        {"run_shell",              true, false, true, false, false},
        // research_fetch_page is already in ask/plan/edit mode_tools directly.
        {"research_fetch_page",    true, true, true, true, true},
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
    // T-lean-floor: agent unions the main-agent file-handling floor
    // (agent_defaults only — NOT shared_defaults) onto a narrowing role
    // card, not just the role card's own tools — fs_write/fs_edit/fs_list/
    // run_shell/research_fetch_page must survive the narrowing, while CAD
    // document-lifecycle tools stay reachable only via enable_tools.
    auto agent_role = copilot_apply_mode_tools(*cm, "agent", {"add_features", "new_part", "delegate"});
    REQUIRE(agent_role.has_value());
    auto agent_has = [&](const char* t) {
        return std::find(agent_role->begin(), agent_role->end(), t) != agent_role->end();
    };
    CHECK(agent_has("add_features"));
    CHECK(agent_has("new_part"));  // explicitly in the role card, not the shared floor
    CHECK(agent_has("delegate"));
    for (const char* t : {"fs_write", "fs_edit", "fs_list", "run_shell", "research_fetch_page"})
        CHECK(agent_has(t));
    // T-lean-floor: SHARED_DEFAULT_TOOLS-only tools must NOT leak onto an
    // agent-mode role card that doesn't request them.
    auto agent_role_narrow = copilot_apply_mode_tools(*cm, "agent", {"add_features"});
    REQUIRE(agent_role_narrow.has_value());
    auto agent_narrow_has = [&](const char* t) {
        return std::find(agent_role_narrow->begin(), agent_role_narrow->end(), t) !=
               agent_role_narrow->end();
    };
    for (const char* t : {"close_part", "new_part", "save_part", "measure_part", "cad_guide",
                          "advise", "import_part", "export_part"})
        CHECK_FALSE(agent_narrow_has(t));
    // Swarm unions the corporate/delegate extras AND the same file-handling
    // floor onto the role set.
    auto swarm_role = copilot_apply_mode_tools(*cm, "swarm", {"add_features"});
    REQUIRE(swarm_role.has_value());
    auto swarm_has = [&](const char* t) {
        return std::find(swarm_role->begin(), swarm_role->end(), t) != swarm_role->end();
    };
    CHECK(swarm_has("add_features"));
    CHECK(swarm_has("delegate"));
    CHECK(swarm_has("corporate_activate"));
    for (const char* t : {"fs_write", "fs_edit", "fs_list", "run_shell", "research_fetch_page"})
        CHECK(swarm_has(t));
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

TEST_CASE("ask/plan do not gain the agent-only file-handling floor") {
    // T-file-floor: fs_write/fs_edit/fs_list/run_shell are the main-agent
    // (agent/swarm-only) floor; ask/plan keep only the read-only shared
    // floor and must never gain them, narrowing role card or not.
    auto cm = CopilotModes::load();
    REQUIRE(cm.has_value());
    auto ask_out = copilot_apply_mode_tools(*cm, "ask", {"describe_part"});
    auto plan_out = copilot_apply_mode_tools(*cm, "plan", {"describe_part"});
    REQUIRE(ask_out.has_value());
    REQUIRE(plan_out.has_value());
    for (const char* t : {"fs_write", "fs_edit", "fs_list", "run_shell"}) {
        CHECK(std::find(ask_out->begin(), ask_out->end(), t) == ask_out->end());
        CHECK(std::find(plan_out->begin(), plan_out->end(), t) == plan_out->end());
    }
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
