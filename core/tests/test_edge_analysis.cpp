#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <cmath>
#include <vector>

#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRep_Builder.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GProp_GProps.hxx>
#include <ShapeUpgrade_UnifySameDomain.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Edge.hxx>
#include <gp_Elips.hxx>
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

namespace {

using chudware::geom::edge_blend_frame;
using chudware::geom::EdgeBlendFrame;

constexpr double kPi = 3.14159265358979323846;

TopTools_IndexedDataMapOfShapeListOfShape edge_faces_of(const TopoDS_Shape& shape) {
    TopTools_IndexedDataMapOfShapeListOfShape map;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, map);
    return map;
}

EdgeBlendFrame frame_of(const TopoDS_Shape& shape, const TopoDS_Edge& edge) {
    const auto frame = edge_blend_frame(edge, edge_faces_of(shape));
    REQUIRE(frame.has_value());
    return *frame;
}

}  // namespace

TEST_CASE("a convex box edge's blend handle sits at its midpoint and pulls into the body") {
    const TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 20, 10).Shape();
    // The +X/+Z edge: both faces' outward normals are (+1,0,0) and (0,0,+1).
    const EdgeBlendFrame top = frame_of(box, find_edge(box, 40, 10, 10, 20));
    CHECK(top.mid[0] == doctest::Approx(40.0));
    CHECK(top.mid[1] == doctest::Approx(10.0));
    CHECK(top.mid[2] == doctest::Approx(10.0));
    REQUIRE(top.grow.has_value());
    CHECK((*top.grow)[0] == doctest::Approx(-std::sqrt(0.5)));
    CHECK(std::abs((*top.grow)[1]) < 1e-9);
    CHECK((*top.grow)[2] == doctest::Approx(-std::sqrt(0.5)));

    // All twelve edges, whatever the winding of their two faces: a unit bisector of two axis
    // faces (two components of magnitude sqrt(1/2)) that leads from the edge into the solid.
    BRepClass3d_SolidClassifier solid(box);
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(box, TopAbs_EDGE, edges);
    REQUIRE(edges.Extent() == 12);
    for (int i = 1; i <= edges.Extent(); ++i) {
        const EdgeBlendFrame frame = frame_of(box, TopoDS::Edge(edges.FindKey(i)));
        REQUIRE(frame.grow.has_value());
        const auto& g = *frame.grow;
        CHECK(std::abs(g[0]) + std::abs(g[1]) + std::abs(g[2]) == doctest::Approx(2.0 * std::sqrt(0.5)));
        CHECK(g[0] * g[0] + g[1] * g[1] + g[2] * g[2] == doctest::Approx(1.0));
        solid.Perform(gp_Pnt(frame.mid[0] + 0.1 * g[0], frame.mid[1] + 0.1 * g[1],
                             frame.mid[2] + 0.1 * g[2]), 1e-7);
        CHECK(solid.State() == TopAbs_IN);
    }
}

TEST_CASE("a concave inside corner's blend handle pulls out into the open corner") {
    const TopoDS_Shape bracket = l_bracket();
    // Inside corner x=5, z=5: outward normals (+1,0,0) of the wall and (0,0,+1) of the base. A
    // round there ADDS material, so its face rises along +(nA+nB), away from the body.
    const EdgeBlendFrame inside = frame_of(bracket, find_edge(bracket, 5, 10, 5, 20));
    CHECK(inside.mid[0] == doctest::Approx(5.0));
    CHECK(inside.mid[1] == doctest::Approx(10.0));
    CHECK(inside.mid[2] == doctest::Approx(5.0));
    REQUIRE(inside.grow.has_value());
    CHECK((*inside.grow)[0] == doctest::Approx(std::sqrt(0.5)));
    CHECK(std::abs((*inside.grow)[1]) < 1e-9);
    CHECK((*inside.grow)[2] == doctest::Approx(std::sqrt(0.5)));

    // The convex outer corner x=0, z=0 points the same way in space but into the material:
    // the probe point decides, for both edges, which side the round works on.
    BRepClass3d_SolidClassifier solid(bracket);
    auto probe = [&](const EdgeBlendFrame& frame) {
        const auto& g = *frame.grow;
        solid.Perform(gp_Pnt(frame.mid[0] + 0.1 * g[0], frame.mid[1] + 0.1 * g[1],
                             frame.mid[2] + 0.1 * g[2]), 1e-7);
        return solid.State();
    };
    CHECK(probe(inside) == TopAbs_OUT);
    const EdgeBlendFrame outer = frame_of(bracket, find_edge(bracket, 0, 10, 0, 20));
    REQUIRE(outer.grow.has_value());
    CHECK(probe(outer) == TopAbs_IN);
}

TEST_CASE("a circular rim's handle sits at half its arc and a seam has a point but no direction") {
    const TopoDS_Shape cylinder = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 5.0, 10.0).Shape();
    const EdgeBlendFrame rim = frame_of(cylinder, find_edge(cylinder, 0, 0, 10, 2.0 * kPi * 5.0));
    CHECK(std::hypot(rim.mid[0], rim.mid[1]) == doctest::Approx(5.0));
    CHECK(rim.mid[2] == doctest::Approx(10.0));
    REQUIRE(rim.grow.has_value());
    const auto& g = *rim.grow;
    // Faces: the +Z cap and the lateral wall, whose outward normal here is radial.
    CHECK(g[2] == doctest::Approx(-std::sqrt(0.5)));
    CHECK((g[0] * rim.mid[0] + g[1] * rim.mid[1]) / 5.0 == doctest::Approx(-std::sqrt(0.5)));

    // The wall's seam is one face on both sides: it has a mid-point and no bisector.
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(cylinder, TopAbs_EDGE, edges);
    int seams = 0;
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges.FindKey(i));
        if (BRepAdaptor_Curve(edge).GetType() != GeomAbs_Line) continue;
        ++seams;
        const EdgeBlendFrame seam = frame_of(cylinder, edge);
        CHECK(seam.mid[2] == doctest::Approx(5.0));
        CHECK_FALSE(seam.grow.has_value());
    }
    CHECK(seams == 1);
}

TEST_CASE("the handle point is at half the ARC LENGTH, and an edge on no face has no direction") {
    // A quarter of a 10 x 2 ellipse: its parameter midpoint (pi/4) is nowhere near its length
    // midpoint, so a parameter-midpoint implementation fails here.
    const gp_Elips ellipse(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 10.0, 2.0);
    const TopoDS_Edge arc = BRepBuilderAPI_MakeEdge(ellipse, 0.0, kPi / 2.0).Edge();
    const auto frame = edge_blend_frame(arc, TopTools_IndexedDataMapOfShapeListOfShape());
    REQUIRE(frame.has_value());
    CHECK_FALSE(frame->grow.has_value());
    // Independent length integral: find the parameter where half the quarter's length is reached.
    const int steps = 400000;
    std::vector<double> cumulative(static_cast<std::size_t>(steps) + 1, 0.0);
    for (int i = 1; i <= steps; ++i) {
        const double a = kPi / 2.0 * (i - 1) / steps, b = kPi / 2.0 * i / steps;
        cumulative[static_cast<std::size_t>(i)] = cumulative[static_cast<std::size_t>(i) - 1] +
            std::hypot(10.0 * (std::cos(b) - std::cos(a)), 2.0 * (std::sin(b) - std::sin(a)));
    }
    std::size_t at = 0;
    while (cumulative[at] < 0.5 * cumulative.back()) ++at;
    const double t = kPi / 2.0 * static_cast<double>(at) / steps;
    CHECK(frame->mid[0] == doctest::Approx(10.0 * std::cos(t)).epsilon(1e-4));
    CHECK(frame->mid[1] == doctest::Approx(2.0 * std::sin(t)).epsilon(1e-4));
    CHECK(std::abs(t - kPi / 4.0) > 0.1);

    // A straight free edge: midpoint only.
    const TopoDS_Edge free_edge = BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(10, 0, 0)).Edge();
    const auto line = edge_blend_frame(free_edge, TopTools_IndexedDataMapOfShapeListOfShape());
    REQUIRE(line.has_value());
    CHECK(line->mid[0] == doctest::Approx(5.0));
    CHECK_FALSE(line->grow.has_value());
}
