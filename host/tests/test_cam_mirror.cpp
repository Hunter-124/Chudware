// Pure-CPU regression tests for the CAM tree/toolpath mirror and simulation-field mesh parser.
// This executable deliberately links no renderer, OpenGL, GLFW, daemon, or fixture data.
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "mesh.h"
#include "ui/cam/cam_mirror.h"

namespace {

using nlohmann::json;

[[noreturn]] void fail(const char* expression, const char* file, int line) {
    std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", file, line, expression);
    std::exit(1);
}

#define CHECK(expression) ((expression) ? static_cast<void>(0) : fail(#expression, __FILE__, __LINE__))

bool near(double actual, double expected, double tolerance = 1.0e-5) {
    return std::fabs(actual - expected) <= tolerance;
}

json treeResponse(int revision, double rapidXY, double rapidZ) {
    return {
        {"revision", revision},
        {"tree", {
            {"name", "Bracket CAM"},
            {"post", "linuxcnc"},
            {"tools", json::array({{
                {"number", 7}, {"name", "6 mm flat"}, {"type", "endmill_flat"},
                {"diameter", 6.0}
            }})},
            {"setups", json::array({{
                {"id", "setup-1"},
                {"name", "Top side"},
                {"machine", "3axis"},
                {"machine_detail", {{"rapid_xy", rapidXY}, {"rapid_z", rapidZ}}},
                {"stock", {{"min", json::array({-2.0, -3.0, -4.0})},
                           {"max", json::array({12.0, 13.0, 14.0})}}},
                {"operations", json::array({{
                    {"id", "op-pocket"},
                    {"name", "Finish pocket"},
                    {"type", "pocket2d"},
                    {"tool_number", 7},
                    {"suppressed", true},
                    {"stepover", 1.25},
                    {"stepdown", 2.5},
                    {"stock_to_leave", 0.35},
                    {"feed", 640.0},
                    {"rpm", 12345.0},
                    {"heights", {
                        {"top", 0.75},
                        {"bottom", -8.5},
                        {"clearance", 18.0},
                        {"retract", 4.25}
                    }}
                }})}
            }})}
        }}
    };
}

json oneCutToolpath() {
    return {{"toolpaths", json::array({{
        {"moves", json::array({
            {{"kind", "rapid"}, {"to", json::array({0.0, 0.0, 0.0})}},
            {{"kind", "cut"}, {"to", json::array({1.0, 0.0, 0.0})}, {"feed", 60.0}}
        })}
    }})}};
}

void testTreeReloadResetsMachineAndLoadsOperationValues() {
    ui::CamMirror mirror;
    mirror.loadTree(treeResponse(10, 1200.0, 450.0));

    CHECK(mirror.revision() == 10);
    CHECK(mirror.post() == "linuxcnc");
    CHECK(near(mirror.rapidXY, 1200.0));
    CHECK(near(mirror.rapidZ, 450.0));
    CHECK(mirror.setups().size() == 1);
    CHECK(mirror.setups()[0].ops.size() == 1);

    const ui::CamOpRow& op = mirror.setups()[0].ops[0];
    CHECK(op.id == "op-pocket");
    CHECK(op.name == "Finish pocket");
    CHECK(op.type == "pocket2d");
    CHECK(op.tool_number == 7);
    CHECK(op.suppressed);
    CHECK(near(op.stepover, 1.25));
    CHECK(near(op.stepdown, 2.5));
    CHECK(near(op.stock_leave, 0.35));
    CHECK(near(op.feed, 640.0));
    CHECK(near(op.rpm, 12345.0));
    CHECK(near(op.top, 0.75));
    CHECK(near(op.bottom, -8.5));
    CHECK(near(op.clearance, 18.0));
    CHECK(near(op.retract, 4.25));

    // A newly loaded tree is authoritative: it must accept the replacement machine rather than
    // retaining the first setup's non-default values.
    mirror.loadTree(treeResponse(11, 999.0, 888.0));
    CHECK(near(mirror.rapidXY, 999.0));
    CHECK(near(mirror.rapidZ, 888.0));

    // Missing machine_detail then restores the documented fallback instead of inheriting rates.
    json noMachineDetail = treeResponse(12, 111.0, 222.0);
    noMachineDetail["tree"]["setups"][0].erase("machine_detail");
    mirror.loadTree(noMachineDetail);
    CHECK(near(mirror.rapidXY, 5000.0));
    CHECK(near(mirror.rapidZ, 3000.0));
}

void testTreeRevisionInvalidatesStaleToolpaths() {
    ui::CamMirror mirror;
    mirror.loadTree(treeResponse(20, 60.0, 60.0));
    mirror.loadToolpaths(oneCutToolpath());
    CHECK(!mirror.backplot().empty());
    CHECK(mirror.backplot().totalTime > 0.0);

    mirror.loadTree(treeResponse(21, 60.0, 60.0));
    CHECK(mirror.revision() == 21);
    CHECK(mirror.backplot().empty());
    CHECK(mirror.backplot().rapidT0.empty());
    CHECK(mirror.backplot().rapidT.empty());
    CHECK(mirror.backplot().cutT0.empty());
    CHECK(mirror.backplot().cutT.empty());
    CHECK(mirror.backplot().arcT0.empty());
    CHECK(mirror.backplot().arcT.empty());
    CHECK(near(mirror.backplot().totalTime, 0.0));
}

void testInterleavedClassesKeepGlobalTimesAndArcLength() {
    ui::CamMirror mirror;
    mirror.loadTree(treeResponse(30, 60.0, 60.0));
    mirror.loadToolpaths({{"toolpaths", json::array({{
        {"moves", json::array({
            // The first point establishes the start pose and creates no segment.
            {{"kind", "rapid"}, {"to", json::array({0.0, 0.0, 0.0})}},
            // Each one-millimetre straight move takes one second at 60 mm/min.
            {{"kind", "rapid"}, {"to", json::array({1.0, 0.0, 0.0})}},
            {{"kind", "cut"}, {"to", json::array({2.0, 0.0, 0.0})}, {"feed", 60.0}},
            {{"kind", "rapid"}, {"to", json::array({2.0, 1.0, 0.0})}},
            // Radius-one quarter circle: duration is pi/2 seconds, not its sqrt(2) chord.
            {{"kind", "arc"}, {"to", json::array({1.0, 2.0, 0.0})},
             {"center", json::array({1.0, 1.0, 0.0})}, {"ccw", true}, {"feed", 60.0}},
            {{"kind", "cut"}, {"to", json::array({0.0, 2.0, 0.0})}, {"feed", 60.0}}
        })}
    }})}});

    const ui::CamBackplot& bp = mirror.backplot();
    CHECK(bp.rapidT0.size() == 2);
    CHECK(bp.rapidT.size() == 2);
    CHECK(bp.cutT0.size() == 2);
    CHECK(bp.cutT.size() == 2);
    CHECK(!bp.arcT0.empty());
    CHECK(bp.arcT0.size() == bp.arcT.size());
    CHECK(bp.rapidT0.size() * 6 == bp.rapid.size());
    CHECK(bp.rapidT.size() * 6 == bp.rapid.size());
    CHECK(bp.cutT0.size() * 6 == bp.cut.size());
    CHECK(bp.cutT.size() * 6 == bp.cut.size());
    CHECK(bp.arcT0.size() * 6 == bp.arc.size());
    CHECK(bp.arcT.size() * 6 == bp.arc.size());

    CHECK(near(bp.rapidT0[0], 0.0));
    CHECK(near(bp.rapidT[0], 1.0));
    CHECK(near(bp.cutT0[0], 1.0));
    CHECK(near(bp.cutT[0], 2.0));
    // The second rapid starts after the intervening cut, not at the previous rapid's end.
    CHECK(near(bp.rapidT0[1], 2.0));
    CHECK(near(bp.rapidT[1], 3.0));
    CHECK(near(bp.arcT0.front(), 3.0));

    for (std::size_t i = 0; i < bp.arcT.size(); ++i) {
        CHECK(bp.arcT[i] > bp.arcT0[i]);
        if (i > 0) CHECK(near(bp.arcT0[i], bp.arcT[i - 1]));
    }

    const double arcDuration = bp.arcT.back() - bp.arcT0.front();
    CHECK(near(arcDuration, std::acos(-1.0) / 2.0, 2.0e-5));
    CHECK(near(bp.cutT0[1], 3.0 + std::acos(-1.0) / 2.0, 2.0e-5));
    CHECK(near(bp.cutT[1], 4.0 + std::acos(-1.0) / 2.0, 2.0e-5));
    CHECK(near(bp.totalTime, 4.0 + std::acos(-1.0) / 2.0, 2.0e-5));
}

bool sameMeshGeometry(const Mesh& a, const Mesh& b) {
    return a.positions == b.positions && a.normals == b.normals && a.indices == b.indices &&
           a.edgeLines == b.edgeLines &&
           a.bbMin.x == b.bbMin.x && a.bbMin.y == b.bbMin.y && a.bbMin.z == b.bbMin.z &&
           a.bbMax.x == b.bbMax.x && a.bbMax.y == b.bbMax.y && a.bbMax.z == b.bbMax.z &&
           a.center.x == b.center.x && a.center.y == b.center.y && a.center.z == b.center.z &&
           a.radius == b.radius;
}

void checkMalformedFieldPreservesMesh(const json& field, const Mesh& expected) {
    Mesh actual = expected;
    bool accepted = true;
    try {
        accepted = ui::camFieldToMesh(field, actual);
    } catch (const std::exception&) {
        CHECK(false && "malformed simulation fields must be rejected, not thrown");
    }
    CHECK(!accepted);
    CHECK(sameMeshGeometry(actual, expected));
}

void testMalformedSimulationFieldsPreservePriorMesh() {
    const json valid = {
        {"nx", 3}, {"ny", 2}, {"res", 0.5}, {"lo", json::array({10.0, 20.0})},
        {"z", json::array({1.0, 1.5, 2.0, 0.5, 1.0, 1.5})}
    };
    Mesh prior;
    CHECK(ui::camFieldToMesh(valid, prior));
    CHECK(!prior.empty());
    prior.edgeLines = {91.0f, 92.0f, 93.0f};
    prior.bbMin = {-5.0f, -6.0f, -7.0f};
    prior.bbMax = {5.0f, 6.0f, 7.0f};
    prior.center = {1.0f, 2.0f, 3.0f};
    prior.radius = 42.0f;
    const Mesh expected = prior;

    json shortGrid = valid;
    shortGrid["z"] = json::array({1.0, 2.0});
    checkMalformedFieldPreservesMesh(shortGrid, expected);

    json badDimension = valid;
    badDimension["nx"] = "three";
    checkMalformedFieldPreservesMesh(badDimension, expected);

    json badOrigin = valid;
    badOrigin["lo"][0] = "left";
    checkMalformedFieldPreservesMesh(badOrigin, expected);

    // This reaches the old parser's mutation point before the invalid sample is read; the
    // all-or-nothing contract prevents a failed frame from erasing the last renderable mesh.
    json badSample = valid;
    badSample["z"][4] = "not-a-height";
    checkMalformedFieldPreservesMesh(badSample, expected);
}

}  // namespace

int main() {
    testTreeReloadResetsMachineAndLoadsOperationValues();
    testTreeRevisionInvalidatesStaleToolpaths();
    testInterleavedClassesKeepGlobalTimesAndArcLength();
    testMalformedSimulationFieldsPreservePriorMesh();
    std::puts("test_cam_mirror PASS");
    return 0;
}
