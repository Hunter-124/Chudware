#include "geom/geometry_analysis.h"

#include <algorithm>
#include <cmath>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <BRep_Tool.hxx>
#include <GProp_GProps.hxx>
#include <Geom2d_Curve.hxx>
#include <Standard_Failure.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Vertex.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Vec.hxx>

namespace chudware::geom {
namespace {
using json = nlohmann::json;

// Omitted thresholds follow the body's diagonal; explicitly supplied zero disables
// a size check. Sliver ratio is dimensionless area / perimeter squared.
double threshold(const json& options, const char* key, double fallback, bool allow_zero = true) {
    if (!options.contains(key)) return fallback;
    if (!options[key].is_number()) throw std::invalid_argument(std::string(key) + " must be numeric");
    const double value = options[key].get<double>();
    if (!std::isfinite(value) || value < 0 || (!allow_zero && value == 0))
        throw std::invalid_argument(std::string(key) + " must be finite and nonnegative");
    return value;
}

double edge_length(const TopoDS_Edge& e) {
    GProp_GProps p;
    BRepGProp::LinearProperties(e, p);
    return p.Mass();
}

// Evaluate a face's outward normal on the edge's own pcurve, at an interior
// parameter (never at a pole/end vertex). Opposite face orientations are honored.
bool edge_normal(const TopoDS_Edge& edge, const TopoDS_Face& face, double fraction,
                 gp_Vec& normal) {
    Standard_Real first = 0, last = 0;
    auto curve = BRep_Tool::CurveOnSurface(edge, face, first, last);
    if (curve.IsNull() || !std::isfinite(first) || !std::isfinite(last) || first == last)
        return false;
    const gp_Pnt2d uv = curve->Value(first + fraction * (last - first));
    auto surface = BRep_Tool::Surface(face);
    if (surface.IsNull()) return false;
    gp_Pnt p;
    gp_Vec du, dv;
    surface->D1(uv.X(), uv.Y(), p, du, dv);
    normal = du.Crossed(dv);
    if (normal.SquareMagnitude() < 1e-24) return false;
    if (face.Orientation() == TopAbs_REVERSED) normal.Reverse();
    normal.Normalize();
    return true;
}

struct FreeEnd {
    int edge_id;
    gp_Pnt point;
};

json analyze_body(const TopoDS_Shape& shape, int body_id, const json& options) {
    Bnd_Box box;
    BRepBndLib::AddOptimal(shape, box, Standard_False, Standard_False);
    box.SetGap(0);
    Standard_Real x0, y0, z0, x1, y1, z1;
    box.Get(x0, y0, z0, x1, y1, z1);
    const double diag = std::hypot(std::hypot(x1 - x0, y1 - y0), z1 - z0);
    const double area_limit = threshold(options, "min_face_area", std::max(1e-6, diag * diag * 1e-4));
    const double edge_limit = threshold(options, "min_edge_length", std::max(1e-4, diag * 1e-3));
    const double ratio_limit = threshold(options, "sliver_ratio", 0.001);
    const double gap_limit = threshold(options, "gap_tol", std::max(1e-4, diag * 1e-4));
    if (options.contains("include_discontinuities") &&
        !options["include_discontinuities"].is_boolean())
        throw std::invalid_argument("include_discontinuities must be boolean");
    const bool discontinuities = options.value("include_discontinuities", false);

    TopTools_IndexedMapOfShape faces, edges;
    TopExp::MapShapes(shape, TopAbs_FACE, faces);
    TopExp::MapShapes(shape, TopAbs_EDGE, edges);
    TopTools_IndexedDataMapOfShapeListOfShape owners;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, owners);
    const bool valid = BRepCheck_Analyzer(shape).IsValid();
    json findings = json::array();
    auto add = [&](const char* kind, int face, int edge, double value, double limit) {
        findings.push_back({{"kind", kind}, {"body_id", body_id}, {"face_id", face},
                            {"edge_id", edge}, {"value", value}, {"threshold", limit}});
    };
    if (!valid) add("brep_invalid", 0, 0, 0, 0);

    for (int i = 1; i <= faces.Extent(); ++i) {
        const auto face = TopoDS::Face(faces.FindKey(i));
        GProp_GProps p;
        BRepGProp::SurfaceProperties(face, p);
        const double area = p.Mass();
        if (area_limit > 0 && area < area_limit) add("small_face", i, 0, area, area_limit);
        // A face with long, narrow edges is a sliver even if its area is above
        // the absolute small-face threshold. Degenerate edges do not add length.
        if (ratio_limit > 0 && area > 0) {
            double perimeter = 0;
            for (TopExp_Explorer e(face, TopAbs_EDGE); e.More(); e.Next())
                if (!BRep_Tool::Degenerated(TopoDS::Edge(e.Current())))
                    perimeter += edge_length(TopoDS::Edge(e.Current()));
            if (perimeter > 0 && area / (perimeter * perimeter) < ratio_limit)
                add("sliver_face", i, 0, area / (perimeter * perimeter), ratio_limit);
        }
    }

    // Count oriented boundary uses rather than ancestor count: a closed seam has
    // two uses on the same face; INTERNAL imprints aren't open boundary edges.
    std::vector<int> boundary(edges.Extent() + 1, 0);
    std::vector<bool> embedded(edges.Extent() + 1, false);
    for (TopExp_Explorer f(shape, TopAbs_FACE); f.More(); f.Next())
        for (TopExp_Explorer e(f.Current(), TopAbs_EDGE); e.More(); e.Next()) {
            const int id = edges.FindIndex(e.Current());
            if (id < 1) continue;
            if (e.Current().Orientation() == TopAbs_FORWARD || e.Current().Orientation() == TopAbs_REVERSED)
                ++boundary[id];
            else embedded[id] = true;
        }
    std::vector<FreeEnd> free_ends;
    for (int i = 1; i <= edges.Extent(); ++i) {
        const auto edge = TopoDS::Edge(edges.FindKey(i));
        if (BRep_Tool::Degenerated(edge)) continue;
        const double len = edge_length(edge);
        if (edge_limit > 0 && len < edge_limit) add("short_edge", 0, i, len, edge_limit);
        const bool free = boundary[i] == 1 || (boundary[i] == 0 && !embedded[i]);
        if (free) {
            add("open_edge", 0, i, 1, 2);
            TopoDS_Vertex a, b;
            TopExp::Vertices(edge, a, b);
            if (!a.IsNull()) free_ends.push_back({i, BRep_Tool::Pnt(a)});
            if (!b.IsNull()) free_ends.push_back({i, BRep_Tool::Pnt(b)});
        }
        if (!discontinuities || !owners.Contains(edge)) continue;
        const auto& adjacent = owners.FindFromKey(edge);
        if (adjacent.Extent() != 2) continue;
        TopTools_ListIteratorOfListOfShape it(adjacent);
        const auto a = TopoDS::Face(it.Value());
        it.Next();
        const auto b = TopoDS::Face(it.Value());
        if (a.IsSame(b)) continue; // seam; not a face-to-face discontinuity
        double max_angle = 0;
        int samples = 0;
        for (double t : {0.25, 0.5, 0.75}) {
            gp_Vec na, nb;
            try {
                if (!edge_normal(edge, a, t, na) || !edge_normal(edge, b, t, nb)) continue;
                const double dot = std::clamp(na.Dot(nb), -1.0, 1.0);
                max_angle = std::max(max_angle, std::acos(dot) * 180.0 / 3.14159265358979323846);
                ++samples;
            } catch (const Standard_Failure&) { /* singular UV: other samples may succeed */ }
        }
        if (samples && max_angle > 5.0) {
            const int ia = faces.FindIndex(a), ib = faces.FindIndex(b);
            add("non_g1_edge", ia, i, max_angle, 5.0);
            findings.back()["other_face_id"] = ib;
        }
    }
    // Distinct free boundaries whose end points nearly meet but are not sewn.
    // One finding per edge pair, avoiding quadratic work on enormous imported skins.
    std::set<std::pair<int, int>> reported_gaps;
    if (gap_limit > 0 && free_ends.size() <= 1024) {
        for (size_t i = 0; i < free_ends.size(); ++i)
            for (size_t j = i + 1; j < free_ends.size(); ++j) {
                if (free_ends[i].edge_id == free_ends[j].edge_id) continue;
                const double d = free_ends[i].point.Distance(free_ends[j].point);
                if (d <= 1e-7 || d >= gap_limit) continue;
                const auto key = std::minmax(free_ends[i].edge_id, free_ends[j].edge_id);
                if (reported_gaps.insert(key).second) {
                    add("gap", 0, key.first, d, gap_limit);
                    findings.back()["other_edge_id"] = key.second;
                }
            }
    }
    return {{"body_id", body_id}, {"brep_valid", valid}, {"face_count", faces.Extent()},
            {"edge_count", edges.Extent()}, {"findings", std::move(findings)},
            {"thresholds", {{"min_face_area", area_limit}, {"min_edge_length", edge_limit},
                             {"sliver_ratio", ratio_limit}, {"gap_tol", gap_limit}}},
            {"gap_scan_complete", free_ends.size() <= 1024}};
}
} // namespace

json geometry_analysis(const std::vector<TopoDS_Shape>& shapes,
                       const std::vector<int>& body_ids, const json& options) {
    if (!options.is_object() || body_ids.size() != shapes.size())
        throw std::invalid_argument("geometry analysis expects options and parallel body IDs");
    json bodies = json::array();
    for (size_t i = 0; i < shapes.size(); ++i) {
        if (!shapes[i].IsNull()) bodies.push_back(analyze_body(shapes[i], body_ids[i], options));
    }
    return {{"bodies", std::move(bodies)}};
}
} // namespace chudware::geom
