#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <cmath>
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRep_Builder.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <GProp_GProps.hxx>
#include <ShapeUpgrade_UnifySameDomain.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Edge.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include "geom/edge_analysis.h"

using nlohmann::json;
using chudware::geom::edge_analysis;

namespace {

// L-bracket: a 40x20x5 base with a 5x20x30 wall on its -x end (profile in XZ, depth along
// Y). The one inside corner is the edge x=5, z=5, y in [0, 20].
TopoDS_Shape l_bracket() {
    const TopoDS_Shape base = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 20, 5).Shape();
    const TopoDS_Shape wall = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 5, 20, 30).Shape();
    BRepAlgoAPI_Fuse fuse(base, wall);
    fuse.Build();
    REQUIRE(fuse.IsDone());
    ShapeUpgrade_UnifySameDomain unify(fuse.Shape(), true, true, true);
    unify.Build();
    return unify.Shape();
}

TopoDS_Edge find_edge(const TopoDS_Shape& shape, double x, double y, double z, double length) {
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(shape, TopAbs_EDGE, edges);
    for (int i = 1; i <= edges.Extent(); ++i) {
        GProp_GProps props;
        BRepGProp::LinearProperties(edges.FindKey(i), props);
        const gp_Pnt c = props.CentreOfMass();
        if (std::abs(c.X() - x) < 1e-6 && std::abs(c.Y() - y) < 1e-6 &&
            std::abs(c.Z() - z) < 1e-6 && std::abs(props.Mass() - length) < 1e-6)
            return TopoDS::Edge(edges.FindKey(i));
    }
    FAIL("edge not found");
    return TopoDS_Edge();
}

std::vector<json> of_kind(const json& result, const char* kind) {
    std::vector<json> out;
    for (const auto& row : result["edges"])
        if (row["kind"] == kind) out.push_back(row);
    return out;
}

TopoDS_Shape fillet(const TopoDS_Shape& shape, const TopoDS_Edge& edge, double radius) {
    BRepFilletAPI_MakeFillet mk(shape);
    mk.Add(radius, edge);
    mk.Build();
    REQUIRE(mk.IsDone());
    return mk.Shape();
}

} // namespace

TEST_CASE("an L-bracket's sharp inside corner is ONE concave edge at 270 degrees, no blend") {
    const json result = edge_analysis(l_bracket(), json::object());
    CHECK(result["unclassified_edges"].empty());
    const auto concave = of_kind(result, "concave");
    REQUIRE(concave.size() == 1);
    CHECK(concave[0]["dihedral_deg"].get<double>() == doctest::Approx(270.0).epsilon(1e-6));
    CHECK(concave[0]["length_mm"].get<double>() == doctest::Approx(20.0).epsilon(1e-6));
    CHECK(concave[0]["mid"][0].get<double>() == doctest::Approx(5.0));
    CHECK(concave[0]["mid"][1].get<double>() == doctest::Approx(10.0));
    CHECK(concave[0]["mid"][2].get<double>() == doctest::Approx(5.0));
    CHECK(concave[0]["face_ids"].size() == 2);
    CHECK(result["concave_blends"].empty());
    // Everything else on a prismatic L is a 90-degree outside corner.
    const auto convex = of_kind(result, "convex");
    CHECK(convex.size() + 1 == result["edges"].size());
    for (const auto& row : convex)
        CHECK(row["dihedral_deg"].get<double>() == doctest::Approx(90.0).epsilon(1e-6));
}

TEST_CASE("filleting the inside corner replaces the concave edge with a concave blend of that radius") {
    const TopoDS_Shape sharp = l_bracket();
    const TopoDS_Shape filleted = fillet(sharp, find_edge(sharp, 5, 10, 5, 20), 2.0);
    const json result = edge_analysis(filleted, json::object());
    CHECK(result["unclassified_edges"].empty());
    CHECK(of_kind(result, "concave").empty());
    CHECK(of_kind(result, "smooth").size() == 2);  // the two tangent edges of the fillet
    REQUIRE(result["concave_blends"].size() == 1);
    const json& blend = result["concave_blends"][0];
    CHECK(blend["surf"] == "cylinder");
    CHECK(blend["radius_mm"].get<double>() == doctest::Approx(2.0).epsilon(1e-6));
    CHECK(blend["arc_deg"].get<double>() == doctest::Approx(90.0).epsilon(1e-4));
    CHECK(blend["tangent_face_ids"].size() == 2);
    // Zero threshold still identifies exact tangencies, not unknown sign.
    const json zero = edge_analysis(filleted, json{{"min_dihedral_dev_deg", 0.0}});
    CHECK(of_kind(zero, "smooth").size() == 2);
    CHECK(zero["unclassified_edges"].empty());
}

TEST_CASE("a convex round is not a concave blend and a bore is not a blend at all") {
    const TopoDS_Shape sharp = l_bracket();
    // Round the outside corner at the end of the base (x=40, z=5).
    const TopoDS_Shape rounded = fillet(sharp, find_edge(sharp, 40, 10, 5, 20), 1.5);
    const json outside = edge_analysis(rounded, json::object());
    CHECK(outside["concave_blends"].empty());
    CHECK(of_kind(outside, "concave").size() == 1);  // the inside corner is still sharp

    const TopoDS_Shape drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(25, 10, -1), gp_Dir(0, 0, 1)), 3.0, 10.0).Shape();
    BRepAlgoAPI_Cut cut(sharp, drill);
    cut.Build();
    REQUIRE(cut.IsDone());
    const json bored = edge_analysis(cut.Shape(), json::object());
    CHECK(bored["concave_blends"].empty());
    CHECK(of_kind(bored, "concave").size() == 1);
    // The two rim circles are material-side 90-degree corners.
    int rims = 0;
    for (const auto& row : bored["edges"])
        if (row["curve"] == "circle" && row["radius_mm"].get<double>() == doctest::Approx(3.0)) {
            ++rims;
            CHECK(row["kind"] == "convex");
            CHECK(row["dihedral_deg"].get<double>() == doctest::Approx(90.0).epsilon(1e-6));
        }
    CHECK(rims == 2);
}

TEST_CASE("a boss on a plate: sharp circular inside corner, then a torus blend of the fillet radius") {
    const TopoDS_Shape plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 40, 5).Shape();
    const TopoDS_Shape boss = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20, 20, 5), gp_Dir(0, 0, 1)), 8.0, 15.0).Shape();
    BRepAlgoAPI_Fuse fuse(plate, boss);
    fuse.Build();
    REQUIRE(fuse.IsDone());
    const TopoDS_Shape joined = fuse.Shape();

    const json sharp = edge_analysis(joined, json::object());
    const auto concave = of_kind(sharp, "concave");
    REQUIRE(concave.size() == 1);
    CHECK(concave[0]["curve"] == "circle");
    CHECK(concave[0]["radius_mm"].get<double>() == doctest::Approx(8.0).epsilon(1e-6));
    CHECK(concave[0]["dihedral_deg"].get<double>() == doctest::Approx(270.0).epsilon(1e-6));
    CHECK(sharp["concave_blends"].empty());

    TopoDS_Edge junction;
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(joined, TopAbs_EDGE, edges);
    for (int i = 1; i <= edges.Extent(); ++i) {
        BRepAdaptor_Curve c(TopoDS::Edge(edges.FindKey(i)));
        if (c.GetType() == GeomAbs_Circle && std::abs(c.Circle().Radius() - 8.0) < 1e-6 &&
            std::abs(c.Circle().Location().Z() - 5.0) < 1e-6)
            junction = TopoDS::Edge(edges.FindKey(i));
    }
    REQUIRE(!junction.IsNull());
    const json blended = edge_analysis(fillet(joined, junction, 2.0), json::object());
    CHECK(of_kind(blended, "concave").empty());
    REQUIRE(blended["concave_blends"].size() == 1);
    CHECK(blended["concave_blends"][0]["surf"] == "torus");
    CHECK(blended["concave_blends"][0]["radius_mm"].get<double>() == doctest::Approx(2.0).epsilon(1e-6));
}

TEST_CASE("malformed options are refused, not defaulted") {
    CHECK_THROWS_AS(edge_analysis(l_bracket(), json{{"min_dihedral_dev_deg", "five"}}),
                    std::invalid_argument);
    CHECK_THROWS_AS(edge_analysis(l_bracket(), json{{"min_dihedral_dev_deg", 120.0}}),
                    std::invalid_argument);
}

TEST_CASE("ligaments measure finite bore faces against nonincident faces and other bores") {
    const TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 20, 5).Shape();
    const TopoDS_Shape through = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(10, 10, -1), gp_Dir(0, 0, 1)), 2.0, 7.0).Shape();
    BRepAlgoAPI_Cut first(box, through);
    first.Build();
    REQUIRE(first.IsDone());
    // The blind bore's bottom is incident to it and must not yield a zero gap.
    const TopoDS_Shape blind = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(22, 10, 2), gp_Dir(0, 0, 1)), 2.0, 4.0).Shape();
    BRepAlgoAPI_Cut second(first.Shape(), blind);
    second.Build();
    REQUIRE(second.IsDone());
    const json ligaments = edge_analysis(second.Shape(), json{{"include_ligaments", true}})["ligaments"];
    CHECK(ligaments["bore_count"] == 2);
    CHECK_FALSE(ligaments["incomplete"].get<bool>());
    CHECK_FALSE(ligaments["truncated"].get<bool>());
    CHECK(ligaments["total_rows"] == 3);
    int boundary_count = 0, pair_count = 0;
    std::vector<double> boundary_distances;
    for (const json& row : ligaments["rows"]) {
        CHECK(row["basis"] == "minimum_distance_between_finite_faces");
        CHECK(row["face_id"].get<int>() > 0);
        CHECK(row["other_face_id"].get<int>() > 0);
        CHECK(row["face_id"] != row["other_face_id"]);
        CHECK(row["radius_mm"].get<double>() == doctest::Approx(2));
        const double dx = row["point"][0].get<double>() - row["other_point"][0].get<double>();
        const double dy = row["point"][1].get<double>() - row["other_point"][1].get<double>();
        const double dz = row["point"][2].get<double>() - row["other_point"][2].get<double>();
        CHECK(std::sqrt(dx * dx + dy * dy + dz * dz) ==
              doctest::Approx(row["distance_mm"].get<double>()).epsilon(1e-6));
        if (row["kind"] == "bore_boundary") {
            ++boundary_count;
            boundary_distances.push_back(row["distance_mm"].get<double>());
        } else if (row["kind"] == "bore_bore") {
            ++pair_count;
            CHECK(row["other_radius_mm"].get<double>() == doctest::Approx(2));
            CHECK(row["distance_mm"].get<double>() == doctest::Approx(8).epsilon(1e-6));
        }
    }
    CHECK(boundary_count == 2);
    CHECK(pair_count == 1);
    // The blind bore ends at z=2, so its nearest nonincident boundary is the
    // box's bottom face at z=0. The through bore is 8 mm from a side face.
    std::sort(boundary_distances.begin(), boundary_distances.end());
    REQUIRE(boundary_distances.size() == 2);
    CHECK(boundary_distances[0] == doctest::Approx(2).epsilon(1e-6));
    CHECK(boundary_distances[1] == doctest::Approx(8).epsilon(1e-6));
}

TEST_CASE("ligaments do not classify exterior bosses or partial fillets as bores") {
    const TopoDS_Shape plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 40, 5).Shape();
    const TopoDS_Shape boss = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(20, 20, 5), gp_Dir(0, 0, 1)), 8.0, 15.0).Shape();
    BRepAlgoAPI_Fuse fuse(plate, boss);
    fuse.Build();
    REQUIRE(fuse.IsDone());
    const json boss_result = edge_analysis(fuse.Shape(), json{{"include_ligaments", true}})["ligaments"];
    CHECK(boss_result["bore_count"] == 0);
    CHECK(boss_result["rows"].empty());

    const TopoDS_Shape bracket = l_bracket();
    const TopoDS_Shape round = fillet(bracket, find_edge(bracket, 5, 10, 5, 20), 2.0);
    const json fillet_result = edge_analysis(round, json{{"include_ligaments", true}})["ligaments"];
    CHECK(fillet_result["bore_count"] == 0);
    CHECK(fillet_result["rows"].empty());
    CHECK(fillet_result["excluded_partial_cylinders"].get<int>() > 0);
    CHECK(fillet_result["incomplete"] == true);
}

TEST_CASE("ligaments never pair bores from different solids") {
    BRep_Builder builder;
    TopoDS_Compound compound;
    builder.MakeCompound(compound);
    for (double x : {0.0, 50.0}) {
        const TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), 20, 20, 5).Shape();
        const TopoDS_Shape drill = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(x + 10, 10, -1), gp_Dir(0, 0, 1)), 2.0, 7.0).Shape();
        BRepAlgoAPI_Cut cut(box, drill);
        cut.Build();
        REQUIRE(cut.IsDone());
        builder.Add(compound, cut.Shape());
    }
    const json ligaments = edge_analysis(compound, json{{"include_ligaments", true}})["ligaments"];
    CHECK(ligaments["bore_count"] == 2);
    CHECK(ligaments["rows"].size() == 2);
    for (const json& row : ligaments["rows"]) CHECK(row["kind"] == "bore_boundary");
}

TEST_CASE("bore-to-bore distance respects finite depth rather than infinite axes") {
    const TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 20, 10).Shape();
    const TopoDS_Shape upper = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(10, 10, 7), gp_Dir(0, 0, 1)), 2.0, 4.0).Shape();
    BRepAlgoAPI_Cut first(box, upper);
    first.Build();
    REQUIRE(first.IsDone());
    const TopoDS_Shape lower = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(22, 10, -1), gp_Dir(0, 0, 1)), 2.0, 4.0).Shape();
    BRepAlgoAPI_Cut second(first.Shape(), lower);
    second.Build();
    REQUIRE(second.IsDone());
    const json ligaments = edge_analysis(second.Shape(), json{{"include_ligaments", true}})["ligaments"];
    CHECK(ligaments["bore_count"] == 2);
    int pairs = 0;
    for (const json& row : ligaments["rows"]) {
        if (row["kind"] != "bore_bore") continue;
        ++pairs;
        CHECK(row["distance_mm"].get<double>() == doctest::Approx(std::sqrt(80.0)).epsilon(1e-6));
    }
    CHECK(pairs == 1);
}

TEST_CASE("ligament extrema are opt-in and the option is strictly boolean") {
    const TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 20, 20, 5).Shape();
    CHECK((edge_analysis(box, json::object())["ligaments"] == json{{"evaluated", false}}));
    CHECK((edge_analysis(box, json{{"include_ligaments", false}})["ligaments"] ==
           json{{"evaluated", false}}));
    CHECK(edge_analysis(box, json{{"include_ligaments", true}})["ligaments"]["evaluated"] == true);
    CHECK_THROWS_AS(edge_analysis(box, json{{"include_ligaments", "true"}}), std::invalid_argument);
    CHECK_THROWS_AS(edge_analysis(box, json{{"include_ligaments", nullptr}}), std::invalid_argument);
}
