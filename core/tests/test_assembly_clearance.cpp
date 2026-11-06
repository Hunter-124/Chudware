#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>
#include "geom/assembly_clearance.h"
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <gp_Pnt.hxx>
#include <stdexcept>

using nlohmann::json;
using chudware::geom::clearance::Member;
using chudware::geom::clearance::removal_report;
using chudware::geom::clearance::envelope_report;

TEST_CASE("translated box sweep finds an obstruction and proves the opposite direction clear") {
    std::vector<Member> members = {
        {"moving", "Moving", BRepPrimAPI_MakeBox(1, 1, 1).Shape()},
        {"obstacle", "Obstacle", BRepPrimAPI_MakeBox(gp_Pnt(3, 0, 0), 1, 1, 1).Shape()}};
    json params = {{"distance_mm", 4}, {"queries", {{{"component", "moving"},
        {"directions", {{1, 0, 0}, {-1, 0, 0}}}}}}};
    const auto report = removal_report(members, params);
    REQUIRE(report["complete"] == true);
    REQUIRE(report["results"].size() == 2);
    CHECK(report["results"][0]["clear"] == false);
    REQUIRE(report["results"][0]["blockers"].size() == 1);
    CHECK(report["results"][0]["blockers"][0]["component"] == "obstacle");
    CHECK(report["results"][0]["blockers"][0]["overlap_mm3"].get<double>() == doctest::Approx(1.0));
    CHECK(report["results"][1]["clear"] == true);
}

TEST_CASE("envelopes report intersections and unknown geometry") {
    std::vector<Member> members = {{"box", "Box", BRepPrimAPI_MakeBox(2, 2, 2).Shape()},
                                   {"missing", "Missing", TopoDS_Shape()}};
    json params = {{"solids", {{{"id", "tool"}, {"box", {{"min", {1, 1, 1}}, {"max", {3, 3, 3}}}}}}}};
    auto result = envelope_report(members, params);
    CHECK(result["complete"] == false);
    REQUIRE(result["results"][0]["hits"].size() == 1);
    CHECK(result["results"][0]["hits"][0]["overlap_mm3"].get<double>() == doctest::Approx(1));
    REQUIRE(result["results"][0]["failed"].size() == 1);
    CHECK(result["results"][0]["failed"][0]["reason"] == "missing_solid");
}

TEST_CASE("removal refuses malformed directions and never calls missing geometry clear") {
    std::vector<Member> members = {{"moving", "Moving", BRepPrimAPI_MakeBox(1, 1, 1).Shape()},
                                   {"missing", "Missing", TopoDS_Shape()}};
    json params = {{"distance_mm", 1}, {"queries", {{{"component", "moving"}, {"directions", {{1, 0, 0}}}}}}};
    const auto result = removal_report(members, params);
    CHECK(result["complete"] == false);
    CHECK(result["results"][0]["clear"].is_null());
    params["queries"][0]["directions"][0] = {0, 0, 0};
    CHECK_THROWS_AS(removal_report(members, params), std::invalid_argument);
}

TEST_CASE("curved boundary sweep cannot silently miss a blocker") {
    std::vector<Member> members = {
        {"moving", "Cylinder", BRepPrimAPI_MakeCylinder(1, 2).Shape()},
        {"obstacle", "Obstacle", BRepPrimAPI_MakeBox(gp_Pnt(2.5, -0.25, 0.5), 0.5, 0.5, 1).Shape()}};
    json params = {{"distance_mm", 4}, {"queries", {{{"component", "moving"},
        {"directions", {{1, 0, 0}}}}}}};
    const auto result = removal_report(members, params);
    const auto& row = result["results"][0];
    CHECK(row["clear"] != true);
    if (row["clear"].is_null()) {
        CHECK(result["complete"] == false);
        CHECK_FALSE(row["failed"].empty());
    } else {
        CHECK(row["clear"] == false);
        CHECK_FALSE(row["blockers"].empty());
    }
}
