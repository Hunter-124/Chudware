// Drawing provenance (core/doc/drawing.cpp hlr_project): every projected line and circle in
// drawing_get names the model edge it was drawn from — body ordinal, TopExp edge id and the
// stable sid — in exactly doc_query's numbering, so an agent holding a sid can find its
// projection. Built on a 100x10x40 upright plate with four identical Ø6 holes drilled along Y:
// the four holes project as four EQUAL circles in the front view, the case a size match cannot
// tell apart.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/drawing_session.h"
#include "doc/session.h"

using json = nlohmann::json;
namespace doc = chudware::doc;

namespace {
json dimv(double v) { return json{{"value", v}, {"expr", nullptr}}; }

constexpr std::array<double, 4> kHoleX = {-36.0, -12.0, 12.0, 36.0};

std::string make_cross_hole_plate() {
    json nr = doc::doc_new("part", "CrossHolePlate");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Plate"},
             {"params", {{"length", dimv(100)}, {"width", dimv(10)}, {"height", dimv(40)}}}}}},
    });
    for (std::size_t i = 0; i < kHoleX.size(); ++i)
        ops.push_back(json{{"op", "add_feature"},
                           {"feature", {{"type", "cylinder"}, {"name", "Hole" + std::to_string(i + 1)},
                                        {"params", {{"radius", dimv(3)}, {"height", dimv(20)},
                                                    {"at", json::array({kHoleX[i], 0.0, 0.0})},
                                                    {"axis", json::array({0, 1, 0})},
                                                    {"mode", "cut"}}}}}});
    doc::doc_apply_ops(id, -1, ops, false);
    return id;
}

// Returns a pointer INTO `full`: iterate the stored array by reference, never a
// json::value() copy (a pointer into that temporary dangles once the loop ends).
const json* find_view(const json& full, const std::string& view_id) {
    for (const json& sheet : full.at("sheets")) {
        if (!sheet.contains("views")) continue;
        for (const json& view : sheet.at("views"))
            if (view.value("view_id", std::string()) == view_id) return &view;
    }
    return nullptr;
}
}  // namespace

TEST_CASE("drawing_get carries body + TopExp edge id + doc_query sid on every projected edge") {
    const std::string src = make_cross_hole_plate();
    json created = doc::drawing_new(json::object(), json::object(), "Provenance", src);
    const std::string did = created.at("doc_id").get<std::string>();
    const double px = 150.0, py = 150.0;
    json av = doc::drawing_add_view(did, "base", "front", "s1", json(1.0),
                                    json::array({px, py}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();

    // doc_query's own numbering: (body, topo) -> sid for every edge, and the four hole walls.
    std::map<std::pair<int, int>, std::string> edge_sid;
    // Bind the reply first: ranging over doc_query(...).at(...) would iterate a reference into
    // a temporary that dies before the loop body runs.
    const json edges = doc::doc_query(src, json{{"of", "edge"}});
    for (const json& m : edges.at("matches"))
        edge_sid[{m.at("body").get<int>(), m.at("topo").get<int>()}] = m.at("sid").get<std::string>();
    json walls = doc::doc_query(src, json{{"of", "face"}, {"surf", "cylinder"}}).at("matches");
    REQUIRE(walls.size() == kHoleX.size());
    std::vector<std::pair<double, std::string>> wall_sids;
    for (const json& m : walls)
        wall_sids.emplace_back(m.at("centroid")[0].get<double>(), m.at("sid").get<std::string>());
    std::sort(wall_sids.begin(), wall_sids.end());

    const json full = doc::drawing_get(did);
    const json* view = find_view(full, vid);
    REQUIRE(view != nullptr);

    // Every polyline has an aligned source record, and on this all-sharp-edge part every one
    // of them names a real model edge whose sid is the one doc_query hands out.
    for (const char* layer : {"visible", "hidden"}) {
        const json& pls = view->at(layer);
        const json& srcs = view->at(std::string(layer) + "_src");
        REQUIRE(srcs.size() == pls.size());
        for (const json& s : srcs) {
            REQUIRE(s.is_object());
            CHECK(s.at("kind") == "edge");
            const auto key = std::make_pair(s.at("body").get<int>(), s.at("edge_id").get<int>());
            REQUIRE(edge_sid.count(key) == 1);
            CHECK(s.at("sid") == edge_sid.at(key));
        }
    }

    // Each hole's wall sid reaches exactly one visible circle, centred on THAT hole's model x.
    const json& arcs = view->at("arcs");
    REQUIRE(arcs.size() == 2 * kHoleX.size());  // front rim + back rim per through hole
    for (std::size_t k = 0; k < kHoleX.size(); ++k) {
        const std::string& wall = wall_sids[k].second;
        std::set<std::pair<long, long>> centres;
        int visible = 0;
        for (const json& arc : arcs) {
            const json& s = arc.at("src");
            REQUIRE(s.is_object());
            const auto key = std::make_pair(s.at("body").get<int>(), s.at("edge_id").get<int>());
            CHECK(s.at("sid") == edge_sid.at(key));
            const json& faces = s.at("face_sids");
            if (std::find(faces.begin(), faces.end(), json(wall)) == faces.end()) continue;
            const double cx = arc.at("center")[0].get<double>();
            const double cy = arc.at("center")[1].get<double>();
            CHECK(std::fabs((cx - px) - kHoleX[k]) < 1e-6);
            CHECK(std::fabs(arc.at("radius").get<double>() - 3.0) < 1e-6);
            centres.insert({std::lround(cx * 1e4), std::lround(cy * 1e4)});
            if (arc.at("layer") == "visible") ++visible;
        }
        CHECK(centres.size() == 1);
        CHECK(visible == 1);
    }

    CHECK(doc::drawing_close(did));
    CHECK(doc::doc_close(src));
}

TEST_CASE("a section view's cut pieces keep the source edge they were cut from") {
    const std::string src = make_cross_hole_plate();
    json created = doc::drawing_new(json::object(), json::object(), "Provenance Section", src);
    const std::string did = created.at("doc_id").get<std::string>();
    json av = doc::drawing_add_view(did, "base", "front", "s1", json(1.0),
                                    json::array({150.0, 150.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();
    // A horizontal cut through the hole axes (sheet y of the model's z = 0) halves every rim.
    json sec = doc::drawing_add_section(did, vid, json(nullptr),
                                        json::array({json::array({80.0, 150.0}),
                                                     json::array({220.0, 150.0})}),
                                        json(nullptr), json(nullptr), json::array({150.0, 60.0}),
                                        "s1", json(false), json::object());
    const std::string sid_view = sec.at("view_id").get<std::string>();

    std::set<std::string> edge_sids;
    const json edges = doc::doc_query(src, json{{"of", "edge"}});
    for (const json& m : edges.at("matches"))
        edge_sids.insert(m.at("sid").get<std::string>());

    const json full = doc::drawing_get(did);
    const json* view = find_view(full, sid_view);
    REQUIRE(view != nullptr);
    const json& srcs = view->at("visible_src");
    REQUIRE(srcs.size() == view->at("visible").size());
    int traced = 0;
    for (const json& s : srcs) {
        if (s.is_null()) continue;  // an edge the cut generated on its plane
        ++traced;
        CHECK(s.at("body") == 1);
        CHECK(edge_sids.count(s.at("sid").get<std::string>()) == 1);
    }
    CHECK(traced > 0);

    CHECK(doc::drawing_close(did));
    CHECK(doc::doc_close(src));
}
