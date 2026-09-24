#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "cam/audit.h"
#include "cam/op_common.h"
#include "cam/cut/process.h"
#include "cam/session.h"
#include "cam/post.h"
#include "cam/stock_sim.h"
#include "cam/turn/process.h"

#ifndef CHUDWARE_MFG_FIXTURES
#define CHUDWARE_MFG_FIXTURES "fixtures/mfg"
#endif

using namespace chudware::cam;

namespace {

json read_json(const std::string& relative) {
    std::ifstream input(std::string(CHUDWARE_MFG_FIXTURES) + "/" + relative);
    REQUIRE(input.good());
    return json::parse(input);
}

std::string read_text(const std::string& relative) {
    std::ifstream input(std::string(CHUDWARE_MFG_FIXTURES) + "/" + relative);
    REQUIRE(input.good());
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

std::vector<cut::PlasmaEvent> read_plasma(const std::string& relative) {
    std::ifstream input(std::string(CHUDWARE_MFG_FIXTURES) + "/" + relative);
    REQUIRE(input.good());
    std::vector<cut::PlasmaEvent> events;
    std::string line;
    while (std::getline(input, line)) {
        const json row = json::parse(line);
        const auto kind = cut::plasma_event_from_string(row.at("event").get<std::string>());
        REQUIRE(kind.has_value());
        events.push_back({*kind, row.value("state", "unknown") == "ok"});
    }
    return events;
}

ToolLibrary one_turn_tool() {
    Tool tool;
    tool.number = 1;
    tool.name = "OD CNMG";
    tool.type = ToolType::TurningGeneral;
    tool.diameter = 12;
    return ToolLibrary{{tool}};
}

}  // namespace

TEST_CASE("turning uses radius internally and explicit diameter or radius NC semantics") {
    const json part = read_json("turn/constructed_part.json");
    const double radius = part.at("profile_radius_mm").at(1).at(1).get<double>();
    CHECK(turn::program_x(radius, turn::XMode::Diameter) == doctest::Approx(24.0));
    CHECK(turn::program_x(radius, turn::XMode::Radius) == doctest::Approx(12.0));
    CHECK(turn::internal_radius(24.0, turn::XMode::Diameter) == doctest::Approx(radius));

    Machine machine;
    machine.kind = MachineKind::Lathe;
    machine.travel = {80, 0, 300};
    turn::Process process;
    process.feed_mode = turn::FeedMode::PerRevolution;
    process.feed_mm_rev = 0.2;
    process.spindle_mode = turn::SpindleMode::ConstantSurfaceSpeed;
    process.css_m_min = 150;
    process.max_rpm = 3000;
    process.workholding = {true, turn::WorkholdingKind::ThreeJawChuck, 18, 5, 12, 1};
    process.tool = {turn::ToolOrientation::ExternalRight, 0.4, 12.0};
    CHECK(turn::validate(machine, WCS{}, process).ok);
    CHECK(turn::minimum_safe_z(process.workholding) == doctest::Approx(18.0));
    process.internal_cut = true;
    const auto orientation = turn::validate(machine, WCS{}, process);
    CHECK_FALSE(orientation.ok);
    CHECK(orientation.reason == "turn_tool_orientation_incompatible");
    process.internal_cut = false;
    CHECK(turn::rpm_at_radius(process, 10) == doctest::Approx(2387.324).epsilon(0.001));
}

TEST_CASE("lathe post emits G18, feed per rev, CSS, diameter X, G76, and shared M5 footer") {
    Setup setup;
    setup.id = "turn-setup";
    setup.name = "Turn fixture";
    setup.machine.kind = MachineKind::Lathe;
    setup.machine.name = "Offline XZ lathe";
    setup.machine.travel = {80, 0, 300};
    setup.stock.type = StockType::Cylinder;
    setup.stock.radius = 20;
    setup.stock.height = 40;
    setup.stock.base = {0, 0, -40};

    Operation finish;
    finish.id = "finish";
    finish.type = OpType::TurnFinish;
    finish.tool_number = 1;
    finish.surface_speed_mode = "css";
    finish.css = 150;
    finish.max_turn_rpm = 3000;
    finish.extra = {{"feed_mode", "per_rev"}};
    setup.operations.push_back(finish);

    Toolpath finish_path;
    finish_path.op_id = finish.id;
    finish_path.tool_number = 1;
    finish_path.moves = {
        {MoveKind::Rapid, {15, 0, 0}, false, {}, 0, 1200, 0},
        {MoveKind::Cut, {12, 0, -20}, false, {}, 240, 0, 0},
    };
    PostOptions lathe_options;
    lathe_options.overrides = {{"lathe", true}, {"css_clamp", ""}};
    PostResult posted = post_setup(
        setup, one_turn_tool(), "linuxcnc", {finish_path}, lathe_options);
    REQUIRE(posted.ok);
    CHECK(posted.gcode.find("G18") != std::string::npos);
    CHECK(posted.gcode.find("G7") != std::string::npos);
    CHECK(posted.gcode.find("G95") != std::string::npos);
    CHECK(posted.gcode.find("G96 S150 D3000") != std::string::npos);
    CHECK(posted.gcode.find("X24.000") != std::string::npos);
    CHECK(posted.gcode.find("F0.2") != std::string::npos);
    const size_t m5 = posted.gcode.rfind("M5");
    const size_t m30 = posted.gcode.rfind("M30");
    REQUIRE(m5 != std::string::npos);
    REQUIRE(m30 != std::string::npos);
    CHECK(m5 < m30);

    Operation thread;
    thread.id = "thread";
    thread.type = OpType::TurnThread;
    thread.tool_number = 1;
    thread.thread_pitch = 1.5;
    thread.thread_depth = 0.9;
    thread.infeed = 0.3;
    thread.finish_passes = 1;
    thread.heights.bottom = -22;
    thread.geometry = {{"profile", {{0.0, 10.0}, {-22.0, 10.0}}}};
    thread.extra = {{"feed_mode", "per_rev"}};
    setup.operations = {thread};
    Toolpath simulation_path;
    simulation_path.op_id = thread.id;
    simulation_path.tool_number = 1;
    simulation_path.moves = {
        {MoveKind::Rapid, {11, 0, 0}, false, {}, 0, 1000, 0},
        {MoveKind::Cut, {9.1, 0, -22}, false, {}, 1.5, 0, 0},
    };
    posted = post_setup(
        setup, one_turn_tool(), "linuxcnc", {simulation_path}, lathe_options);
    REQUIRE(posted.ok);
    CHECK(posted.gcode.find("G76 P1.5 Z-22.000 I-0.900 J0.300 K0.900 H1") !=
          std::string::npos);
    CHECK(posted.gcode.find("G1 X18.200") == std::string::npos);
}

TEST_CASE("G95 converts internal mm/min feeds at post and audits mm/rev words") {
    Setup setup;
    setup.machine.kind = MachineKind::Lathe;
    setup.stock.type = StockType::Box;
    setup.stock.min = {0, 0, -10};
    setup.stock.max = {20, 10, 0};
    Operation op;
    op.id = "turn";
    op.type = OpType::TurnFinish;
    op.tool_number = 1;
    op.extra = {{"feed_mode", "per_rev"}};
    setup.operations.push_back(op);
    Job job;
    job.setups = {setup};
    job.tools = one_turn_tool();
    OpContext ctx;
    ctx.machine = setup.machine;
    ctx.tool = job.tools.tools.front();
    ctx.material_id = "aluminum";

    Operation automatic = resolved_op(op, ctx);
    REQUIRE(automatic.rpm > 0);
    Toolpath path;
    path.op_id = op.id;
    path.tool_number = 1;
    path.moves = {
        {MoveKind::Rapid, {15, 0, 0}, false, {}, 0, (int)std::lround(automatic.rpm), 0},
        {MoveKind::Cut, {12, 0, -2}, false, {}, automatic.feed, 0, 0},
    };
    PostOptions options;
    options.overrides = {{"lathe", true}};
    PostResult posted = post_setup(setup, job.tools, "linuxcnc", {path}, options);
    REQUIRE(posted.ok);
    json audit = audit_program(job, posted.gcode, "linuxcnc", setup.machine, {}, "cam_post");
    CHECK(audit["ok"] == true);
    Toolpath stopped = path;
    stopped.moves.front().spindle_rpm = 0;
    CHECK_FALSE(post_setup(setup, job.tools, "linuxcnc", {stopped}, options).ok);

    op.rpm = 1000;
    op.feed = 0.2; // explicit G95 feed in mm/rev
    CHECK(resolved_op(op, ctx).feed == doctest::Approx(200.0));
    const std::string bad = "G21\nG95\nS1000 M3\nF120\nG1 X12 Z-2\n";
    audit = audit_program(job, bad, "linuxcnc", setup.machine, {}, "supplied");
    CHECK(audit["ok"] == false);
    bool bad_per_rev = false;
    for (const auto& error : audit["errors"])
        bad_per_rev |= error.get<std::string>().find("G95 feed") != std::string::npos;
    CHECK(bad_per_rev);
    const std::string doc = cam_new(json{{"name", "lathe feeds"}}).at("doc_id").get<std::string>();
    cam_apply_ops(doc, -1, json::array({
        {{"op", "add_setup"}, {"setup", setup.to_json()}},
        {{"op", "add_tool"}, {"tool", job.tools.tools.front().to_json()}},
    }));
    json autofill = cam_feeds(doc, {{"tool_number", 1}});
    CHECK(autofill["feed_mode"] == "per_rev");
    CHECK(autofill["feed"].get<double>() == doctest::Approx(0.12));
    CHECK(cam_close(doc));
}

TEST_CASE("CAM consumers keep refused operation diagnostics after toolpath caching") {
    const std::string doc = cam_new(json{{"name", "incomplete"}}).at("doc_id").get<std::string>();
    const json setup = {{"id", "setup"}, {"name", "S"},
                        {"stock", {{"type", "box"}, {"min", {-20, -20, -5}},
                                   {"max", {20, 20, 0}}}}};
    const json face = {{"id", "face"}, {"type", "face"}, {"tool_number", 1},
                       {"heights", {{"top", 0}, {"bottom", -1}}}};
    const json no_regions = {{"id", "missing"}, {"type", "pocket2d"}, {"tool_number", 1}};
    const json inverted = {{"id", "inverted"}, {"type", "contour2d"}, {"tool_number", 1},
                           {"heights", {{"top", 0}, {"bottom", 2}}},
                           {"geometry", {{"contours", {{{0, 0}, {10, 0}, {10, 10}, {0, 10}}}}}}};
    cam_apply_ops(doc, -1, json::array({
        {{"op", "add_setup"}, {"setup", setup}},
        {{"op", "add_tool"}, {"tool", {{"number", 1}, {"diameter", 6}}}},
        {{"op", "add_operation"}, {"setup_id", "setup"}, {"operation", face}},
        {{"op", "add_operation"}, {"setup_id", "setup"}, {"operation", no_regions}},
        {{"op", "add_operation"}, {"setup_id", "setup"}, {"operation", inverted}},
    }));
    const json fresh_post = cam_post(doc, json::object());
    CHECK(fresh_post["incomplete"] == true);
    CHECK(fresh_post["warnings"].size() == 2);
    const json generated = cam_generate(doc, json::object());
    CHECK(generated["toolpaths"].size() == 1);
    CHECK(generated["warnings"].size() == 2);
    const json post = cam_post(doc, json::object());
    CHECK(post["incomplete"] == true);
    CHECK(post["warnings"].size() == 2);
    const json sim = cam_simulate(doc, json::object());
    REQUIRE(sim["results"].size() == 1);
    CHECK(sim["results"][0]["incomplete"] == true);
    CHECK(sim["results"][0]["warnings"].size() >= 2);
    const json sheet = cam_setup_sheet(doc, json::object());
    CHECK(sheet["incomplete"] == true);
    CHECK(sheet["warnings"].size() == 2);
    const json audit = cam_audit(doc, json::object());
    CHECK(audit["ok"] == false);
    CHECK(audit["errors"].size() >= 2);
    CHECK(cam_close(doc));
}

TEST_CASE("audit rejects spindle-off cutting but not head-based cutting") {
    Job job;
    Setup setup;
    setup.stock.type = StockType::Box;
    setup.stock.min = {0, 0, -5};
    setup.stock.max = {20, 20, 0};
    job.setups = {setup};
    const std::string gcode = "G21\nG94\nF100\nG0 X0 Y0 Z0\nG1 X10 Y0 Z-1\n";
    json result = audit_program(job, gcode, "grbl", setup.machine, {}, "supplied");
    CHECK(result["ok"] == false);
    bool spindle_off = false;
    for (const auto& error : result["errors"])
        spindle_off |= error.get<std::string>().find("spindle is off") != std::string::npos;
    CHECK(spindle_off);
    setup.machine.kind = MachineKind::Laser;
    job.setups.front().machine = setup.machine;
    result = audit_program(job, gcode, "marlin", setup.machine, {}, "supplied");
    CHECK(result["ok"] == true);
}

TEST_CASE("negative operation rpm is rejected instead of suppressing spindle start") {
    bool rejected = false;
    try {
        Operation::from_json({{"id", "negative"}, {"rpm", -1000}});
    } catch (const OpError& error) {
        rejected = error.code == "invalid_op" &&
                   std::string(error.what()).find("rpm") != std::string::npos;
    }
    CHECK(rejected);
}
TEST_CASE("threading produces bounded degressive pass geometry") {

    const json j = read_json("turn/constructed_part.json").at("thread");
    turn::ThreadSpec spec;
    spec.major_radius_mm = j.at("major_radius_mm");
    spec.z_start_mm = j.at("z_start_mm");
    spec.z_end_mm = j.at("z_end_mm");
    spec.pitch_mm = j.at("pitch_mm");
    spec.depth_mm = j.at("depth_mm");
    spec.first_infeed_mm = j.at("first_infeed_mm");
    spec.passes = j.at("passes");
    spec.spring_passes = j.at("spring_passes");
    turn::ValidationResult status;
    const auto passes = turn::generate_thread_passes(spec, &status);
    REQUIRE(status.ok);
    REQUIRE(passes.size() == 5);
    CHECK(passes.front().cut_radius_mm == doctest::Approx(9.7));
    CHECK(passes[1].cut_radius_mm < passes[0].cut_radius_mm);
    CHECK(passes[3].cut_radius_mm == doctest::Approx(9.1));
    CHECK(passes.back().spring);
    CHECK(passes.back().cut_radius_mm == doctest::Approx(9.1));
    for (const auto& pass : passes) {
        CHECK(pass.z_start_mm == doctest::Approx(-2));
        CHECK(pass.z_end_mm == doctest::Approx(-22));
        CHECK(pass.lead_mm == doctest::Approx(20));
    }
}

TEST_CASE("axisymmetric stock simulation carries remnant lineage") {
    Setup setup;
    setup.machine.kind = MachineKind::Lathe;
    setup.machine.travel = {80, 0, 300};
    setup.stock.type = StockType::Cylinder;
    setup.stock.radius = 20;
    setup.stock.height = 40;
    setup.stock.base = {0, 0, -40};
    setup.stock.lineage_id = "stock:turn-fixture-001";
    Toolpath path;
    path.tool_number = 1;
    path.moves = {
        {MoveKind::Rapid, {20, 0, 0}, false, {}, 0, 0, 0},
        {MoveKind::Cut, {15, 0, -40}, false, {}, 100, 0, 0},
    };
    const SimResult sim = simulate_setup(setup, one_turn_tool(), {path}, {}, SimOptions{1.0});
    REQUIRE(sim.ok);
    CHECK(sim.removed_volume_mm3 > 0);
    CHECK(sim.stock_lineage_in == "stock:turn-fixture-001");
    CHECK(sim.stock_lineage_out.rfind("stock:", 0) == 0);
    CHECK(sim.stock_lineage_out != sim.stock_lineage_in);
}

TEST_CASE("generic stock twin refuses cut heads instead of claiming process physics") {
    Setup setup;
    setup.machine.kind = MachineKind::Laser;
    setup.stock.type = StockType::Box;
    setup.stock.min = {0, 0, 0};
    setup.stock.max = {20, 10, 2};
    const SimResult sim = simulate_setup(setup, {}, {}, {}, SimOptions{1.0});
    CHECK_FALSE(sim.ok);
    CHECK(sim.error == "cut_process_physics_unmodelled_use_vector_fidelity_only");
}

TEST_CASE("laser vector post round-trips through independent parser") {
    const json fixture = read_json("cut/laser_path.json");
    cut::LaserProfile profile;
    const json p = fixture.at("profile");
    profile.power_fraction = p.at("power_fraction");
    profile.dynamic_power = p.at("dynamic_power");
    profile.kerf_mm = p.at("kerf_mm");
    profile.feed_mm_min = p.at("feed_mm_min");
    std::vector<Path2> paths;
    for (const auto& encoded_path : fixture.at("paths")) {
        Path2 path;
        for (const auto& point : encoded_path) path.push_back({point[0], point[1]});
        paths.push_back(path);
    }
    cut::Result status;
    const cut::VectorPlan plan = cut::plan_vector(paths, cut::Profile{profile}, &status);
    REQUIRE(status.ok);
    for (const auto& move : plan.moves)
        if (move.setpoint) CHECK(move.setpoint->channel == cut::PowerChannel::LaserFraction);
    const cut::PostResult posted = cut::post_laser_grbl(plan, profile);
    REQUIRE(posted.ok);
    CHECK(posted.nc == read_text("cut/laser_grbl_expected.nc"));
    const cut::ParseReport parsed = cut::parse_laser_grbl(posted.nc);
    REQUIRE(parsed.ok);
    CHECK_FALSE(parsed.power_on_during_traverse);
    REQUIRE(parsed.cut_path.size() == 5);
    CHECK(parsed.cut_path[0].x == doctest::Approx(0));
    CHECK(parsed.cut_path[1].x == doctest::Approx(20));
    CHECK(parsed.cut_path[2].y == doctest::Approx(10));
    CHECK(parsed.cut_path.back().x == doctest::Approx(0));
    CHECK(parsed.cut_path.back().y == doctest::Approx(0));
}

TEST_CASE("plasma replay models pierce, THC locks, and non-protective breakaway") {
    const auto nominal = cut::replay_plasma(read_plasma("cut/plasma_nominal.jsonl"));
    REQUIRE(nominal.size() == 10);
    CHECK(nominal[0].state == cut::PlasmaState::Probing);
    CHECK(nominal[1].state == cut::PlasmaState::Igniting);
    CHECK(nominal[2].state == cut::PlasmaState::Piercing);
    CHECK(nominal[3].state == cut::PlasmaState::Cutting);
    CHECK(nominal[4].thc_enabled);
    CHECK(nominal[5].thc_locked);
    CHECK_FALSE(nominal[5].thc_enabled);
    CHECK(nominal[6].thc_enabled);
    CHECK(nominal[7].thc_locked);
    CHECK(nominal[8].thc_enabled);
    CHECK(nominal.back().state == cut::PlasmaState::Idle);

    const auto broken = cut::replay_plasma(read_plasma("cut/plasma_breakaway.jsonl"));
    REQUIRE_FALSE(broken.empty());
    CHECK(broken.back().state == cut::PlasmaState::Fault);
    CHECK(broken.back().fault == cut::PlasmaFault::Breakaway);
    CHECK_FALSE(broken.back().breakaway_protective_function);
}

TEST_CASE("waterjet exact recipes work but unmodelled compensation refuses by name") {
    const json rows = read_json("cut/waterjet_recipes.json");
    std::vector<cut::WaterjetRecipe> recipes;
    for (const auto& row : rows) {
        cut::WaterjetRecipe recipe;
        recipe.key = {row.at("material"), row.at("thickness_mm"), row.at("orifice_mm"),
                      row.at("mixing_tube_mm")};
        recipe.profile.pressure_mpa = row.at("pressure_mpa");
        recipe.profile.abrasive_g_min = row.at("abrasive_g_min");
        recipe.profile.orifice_mm = row.at("orifice_mm");
        recipe.profile.mixing_tube_mm = row.at("mixing_tube_mm");
        recipe.profile.kerf_mm = row.at("kerf_mm");
        recipe.profile.feed_mm_min = row.at("feed_mm_min");
        recipe.provenance = row.at("provenance");
        recipes.push_back(recipe);
    }
    cut::WaterjetRecipe resolved;
    REQUIRE(cut::resolve_waterjet_recipe(recipes[0].key, recipes, &resolved).ok);
    cut::Result plan_status;
    const auto plan = cut::plan_vector(
        {Path2{{0, 0}, {10, 0}}}, cut::Profile{resolved.profile}, &plan_status);
    REQUIRE(plan_status.ok);
    const auto waterjet_nc = cut::post_waterjet_linuxcnc(plan, resolved.profile);
    REQUIRE(waterjet_nc.ok);
    CHECK(waterjet_nc.nc.find("M67 E0 Q350") != std::string::npos);
    CHECK(waterjet_nc.nc.find("M62 P0") != std::string::npos);
    CHECK(waterjet_nc.nc.find("M64 P1") != std::string::npos);
    cut::WaterjetRecipeKey miss = recipes[0].key;
    miss.thickness_mm = 6.1;
    const auto missing = cut::resolve_waterjet_recipe(miss, recipes, nullptr);
    CHECK_FALSE(missing.ok);
    CHECK(missing.reason == "waterjet_recipe_exact_row_missing");
    resolved.profile.request_taper_compensation = true;
    const auto taper = cut::validate_profile(cut::Profile{resolved.profile});
    CHECK_FALSE(taper.ok);
    CHECK(taper.reason == "waterjet_taper_compensation_unmodelled");
    resolved.profile.request_taper_compensation = false;
    resolved.profile.request_jet_lag_compensation = true;
    const auto lag = cut::validate_profile(cut::Profile{resolved.profile});
    CHECK_FALSE(lag.ok);
    CHECK(lag.reason == "waterjet_jet_lag_unmodelled");
}

TEST_CASE("family operation advertisements are total and unsupported capabilities are named") {
    for (const OpType type : {OpType::TurnFace, OpType::TurnRough, OpType::TurnFinish,
                              OpType::TurnGroove, OpType::TurnThread, OpType::TurnPart,
                              OpType::TurnDrill})
        CHECK(turn::operation_construction_status(type).ok);
    CHECK(cut::operation_construction_status(OpType::Cut2D).ok);
    CHECK(turn::operation_construction_status(OpType::Cut2D).reason ==
          "turn_operation_not_advertised");
    CHECK(cut::operation_construction_status(OpType::TurnFace).reason ==
          "cut_operation_not_advertised");

    for (const auto kind : {cut::ProcessKind::Laser, cut::ProcessKind::Plasma,
                            cut::ProcessKind::Waterjet}) {
        const auto report = cut::capability_report(kind, "art:test");
        REQUIRE(report.complete());
        CHECK(report.levels.at(chudware::device::CapabilityLevel::Execute).truth ==
              chudware::device::CapabilityTruthValue::Unsupported);
        CHECK_FALSE(report.levels.at(chudware::device::CapabilityLevel::Execute).why.empty());
        CHECK(report.levels.at(chudware::device::CapabilityLevel::SafetyIntegration).truth ==
              chudware::device::CapabilityTruthValue::Unsupported);
    }
    const auto turning = turn::capability_report("art:turn");
    REQUIRE(turning.complete());
    CHECK(turning.levels.at(chudware::device::CapabilityLevel::Execute).truth ==
          chudware::device::CapabilityTruthValue::Unsupported);
}
