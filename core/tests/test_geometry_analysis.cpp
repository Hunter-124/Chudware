#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopoDS.hxx>
#include <gp_Pnt.hxx>

#include "geom/geometry_analysis.h"

using nlohmann::json;

static json analyze(const TopoDS_Shape& shape, json options = json::object()) {
    return chudware::geom::geometry_analysis({shape}, {1}, options)["bodies"][0];
}

static bool has(const json& body, const char* kind) {
    for (const auto& row : body["findings"])
        if (row["kind"] == kind) return true;
    return false;
}

TEST_CASE("a clean solid has no default geometry findings; tiny step has selectable small entities") {
    const TopoDS_Shape box = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    const auto clean = analyze(box);
    CHECK(clean["brep_valid"] == true);
    CHECK(clean["findings"].empty());

    BRepAlgoAPI_Fuse fuse(box,
        BRepPrimAPI_MakeBox(gp_Pnt(9.995, 2, 2), 0.01, 1, 1).Shape());
    fuse.Build();
    REQUIRE(fuse.IsDone());
    const auto stepped = analyze(fuse.Shape());
    CHECK(has(stepped, "short_edge"));
    CHECK(has(stepped, "small_face"));
    TopTools_IndexedMapOfShape faces, edges;
    TopExp::MapShapes(fuse.Shape(), TopAbs_FACE, faces);
    TopExp::MapShapes(fuse.Shape(), TopAbs_EDGE, edges);
    for (const auto& row : stepped["findings"]) {
        const int face = row["face_id"], edge = row["edge_id"];
        CHECK(face >= 0);
        CHECK(edge >= 0);
        CHECK(face <= faces.Extent());
        CHECK(edge <= edges.Extent());
    }
}

TEST_CASE("explicit G1 inspection distinguishes sharp corners from fillet tangencies") {
    const TopoDS_Shape box = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    const json options = {{"include_discontinuities", true}};
    CHECK(has(analyze(box, options), "non_g1_edge"));

    TopExp_Explorer exp(box, TopAbs_EDGE);
    REQUIRE(exp.More());
    BRepFilletAPI_MakeFillet fillet(box);
    fillet.Add(2.0, TopoDS::Edge(exp.Current()));
    fillet.Build();
    REQUIRE(fillet.IsDone());
    const auto result = analyze(fillet.Shape(), options);
    TopTools_IndexedMapOfShape edge_ids;
    TopExp::MapShapes(fillet.Shape(), TopAbs_EDGE, edge_ids);
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(fillet.Shape(), TopAbs_EDGE, TopAbs_FACE, edge_faces);
    int tangent_edges = 0;
    for (int i = 1; i <= edge_faces.Extent(); ++i) {
        const auto& fs = edge_faces.FindFromIndex(i);
        if (fs.Extent() != 2) continue;
        bool cylinder = false, plane = false;
        for (TopTools_ListIteratorOfListOfShape it(fs); it.More(); it.Next()) {
            const auto surf = BRepAdaptor_Surface(TopoDS::Face(it.Value())).GetType();
            cylinder |= surf == GeomAbs_Cylinder;
            plane |= surf == GeomAbs_Plane;
        }
        if (!cylinder || !plane) continue;
        const int edge_id = edge_ids.FindIndex(edge_faces.FindKey(i));
        bool flagged = false;
        for (const auto& finding : result["findings"])
            flagged |= finding["kind"] == "non_g1_edge" && finding["edge_id"] == edge_id;
        if (!flagged) ++tangent_edges;
    }
    CHECK(tangent_edges >= 2);
    CHECK(has(result, "non_g1_edge")); // remaining sharp box corners
}
