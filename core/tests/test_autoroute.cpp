// mega-28 E28 — autorouter honours min clearance (no sub-clearance pad/track pairs).
// Pure JSON, OCCT-free; links chudware_ecad only.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "ecad/autoroute.h"
#include "ecad/drc.h"
#include "ecad/schema.h"

#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include <doctest/doctest.h>

using nlohmann::json;
using chudware::ecad::autoroute;
using chudware::ecad::run_drc;

namespace {

json smd_pad(const std::string& pid, double x, double y, const std::string& net,
             double w = 0.3, double h = 0.8) {
    return json{{"id", pid},
                {"number", "1"},
                {"pos", json::array({x, y})},
                {"angle", 0.0},
                {"shape", "rect"},
                {"size", json::array({w, h})},
                {"type", "smd"},
                {"layers", json::array({"F.Cu"})},
                {"drill", nullptr},
                {"net", net}};
}

json fp(const std::string& fid, double x, double y, const json& pads) {
    return json{{"id", fid},
                {"fpid", "T:FP"},
                {"ref", fid},
                {"value", ""},
                {"pos", json::array({x, y})},
                {"angle", 0.0},
                {"side", "top"},
                {"pads", pads},
                {"graphics", json::array()},
                {"model_3d", nullptr},
                {"attr", "smd"},
                {"linked_symbol", nullptr}};
}

json rect_outline(double w, double h) {
    json shapes = json::array();
    shapes.push_back(json{{"type", "line"}, {"p1", json::array({0.0, 0.0})}, {"p2", json::array({w, 0.0})}});
    shapes.push_back(json{{"type", "line"}, {"p1", json::array({w, 0.0})}, {"p2", json::array({w, h})}});
    shapes.push_back(json{{"type", "line"}, {"p1", json::array({w, h})}, {"p2", json::array({0.0, h})}});
    shapes.push_back(json{{"type", "line"}, {"p1", json::array({0.0, h})}, {"p2", json::array({0.0, 0.0})}});
    return json{{"shapes", shapes}};
}

// Two QFN-like footprints with interleaved nets — the mega-24..27 clearance stress shape.
json tight_ic_board() {
    json b = chudware::ecad::schema::new_board("e28");
    b["rules"]["clearance"] = 0.2;
    b["net_classes"]["default"] = json{{"clearance", 0.2},
                                       {"track_width", 0.15},
                                       {"via_diameter", 0.8},
                                       {"via_drill", 0.4}};
    for (const char* n : {"3V3", "GND", "N$2", "N$9", "N$10", "SIG"}) {
        bool have = false;
        for (const auto& nn : b["nets"])
            if (nn.value("name", std::string()) == n) have = true;
        if (!have) b["nets"].push_back(json{{"name", n}, {"class", "default"}});
    }
    b["outline"] = rect_outline(80.0, 60.0);
    const double pin_pitch = 0.5;
    const double side = pin_pitch * 2 + 1.0;
    auto pads_for = [&](const std::string& prefix, const std::vector<std::string>& cycle) {
        json pads = json::array();
        for (int i = 0; i < 2; ++i) {
            double x = -0.5 * pin_pitch + i * pin_pitch;
            pads.push_back(smd_pad(prefix + "b" + std::to_string(i), x, -side / 2.0,
                                   cycle[static_cast<size_t>(i) % cycle.size()]));
        }
        for (int i = 0; i < 2; ++i) {
            double x = -0.5 * pin_pitch + i * pin_pitch;
            pads.push_back(smd_pad(prefix + "t" + std::to_string(i), x, side / 2.0,
                                   cycle[static_cast<size_t>(i + 2) % cycle.size()]));
        }
        return pads;
    };
    std::vector<std::string> cycle{"3V3", "GND", "N$2", "N$9", "N$10", "SIG"};
    b["footprints"] = json::array({fp("U1", 20.0, 30.0, pads_for("u1", cycle)),
                                   fp("U2", 40.0, 30.0, pads_for("u2", cycle))});
    return b;
}

int clearance_errors(const json& board) {
    json viols = run_drc(board);
    int n = 0;
    for (const auto& v : viols) {
        if (v.value("rule", std::string()) == "clearance" &&
            v.value("severity", std::string()) == "error")
            ++n;
    }
    return n;
}

json apply_route(const json& board, const json& res) {
    json routed = board;
    if (!routed.contains("tracks") || !routed["tracks"].is_array()) routed["tracks"] = json::array();
    if (!routed.contains("vias") || !routed["vias"].is_array()) routed["vias"] = json::array();
    for (const auto& t : res.at("tracks")) routed["tracks"].push_back(t);
    for (const auto& v : res.at("vias")) routed["vias"].push_back(v);
    return routed;
}

}  // namespace

TEST_CASE("autoroute: routed geometry respects 0.2mm clearance (E28)") {
    json board = tight_ic_board();
    json res = autoroute(board, json{{"pitch", 0.25}, {"min_clearance", 0.2}});
    REQUIRE(res.contains("tracks"));
    REQUIRE(res.contains("report"));
    json routed = apply_route(board, res);
    const int n_err = clearance_errors(routed);
    CHECK_MESSAGE(n_err == 0, "expected 0 clearance DRC errors, got " << n_err
                               << " report=" << res["report"].dump());
    CHECK(res["report"].contains("routed"));
    CHECK_FALSE(res["report"]["routed"].empty());
}

TEST_CASE("autoroute: segment pairs keep edge gap >= min_clearance (E28 spacing)") {
    json board = chudware::ecad::schema::new_board("e28gap");
    board["rules"]["clearance"] = 0.2;
    board["net_classes"]["default"] = json{{"clearance", 0.2},
                                           {"track_width", 0.25},
                                           {"via_diameter", 0.8},
                                           {"via_drill", 0.4}};
    for (const char* n : {"A", "B"})
        board["nets"].push_back(json{{"name", n}, {"class", "default"}});
    board["outline"] = rect_outline(40.0, 30.0);
    board["footprints"] = json::array({
        fp("fp1", 8.0, 15.0,
           json::array({smd_pad("p1", 0.0, 2.0, "A", 0.6, 0.6),
                        smd_pad("p2", 0.0, -2.0, "B", 0.6, 0.6)})),
        fp("fp2", 32.0, 15.0,
           json::array({smd_pad("p3", 0.0, 2.0, "A", 0.6, 0.6),
                        smd_pad("p4", 0.0, -2.0, "B", 0.6, 0.6)})),
    });
    json res = autoroute(board, json{{"pitch", 0.25}, {"min_clearance_mm", 0.2}});
    json routed = apply_route(board, res);
    CHECK(clearance_errors(routed) == 0);
}
