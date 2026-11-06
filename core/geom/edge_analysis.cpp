#include "geom/edge_analysis.h"

#include <algorithm>
#include <cmath>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepGProp.hxx>
#include <BRepTools.hxx>
#include <BRep_Tool.hxx>
#include <GProp_GProps.hxx>
#include <Geom2d_Curve.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <Geom_Surface.hxx>
#include <Standard_Failure.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <gp_Ax1.hxx>
#include <gp_Ax3.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Sphere.hxx>
#include <gp_Circ.hxx>
#include <gp_Torus.hxx>
#include <gp_Vec.hxx>

namespace chudware::geom {
namespace {
using json = nlohmann::json;

constexpr double kPi = 3.14159265358979323846;
constexpr double kRadToDeg = 180.0 / kPi;
// Interior fractions of the edge's parameter range; the ends are never sampled (poles,
// vertices and blend run-outs make the normals there unreliable).
constexpr double kSamples[] = {0.1, 0.3, 0.5, 0.7, 0.9};

double r6(double x) { return std::abs(x) < 1e-9 ? 0.0 : std::round(x * 1e6) / 1e6; }
json vec3(double x, double y, double z) { return json::array({r6(x), r6(y), r6(z)}); }
json vec3(const gp_Pnt& p) { return vec3(p.X(), p.Y(), p.Z()); }
json vec3(const gp_Vec& v) { return vec3(v.X(), v.Y(), v.Z()); }
json vec3(const gp_Dir& d) { return vec3(d.X(), d.Y(), d.Z()); }

// Outward unit normal of `face` on `edge`'s own pcurve at `fraction` of the parameter
// range. The face orientation (as handed to us by the solid's ancestor map) flips the
// surface normal, so the result points out of the material.
bool outward_normal(const TopoDS_Edge& edge, const TopoDS_Face& face, double fraction,
                    gp_Vec& normal) {
    Standard_Real first = 0, last = 0;
    const auto pcurve = BRep_Tool::CurveOnSurface(edge, face, first, last);
    if (pcurve.IsNull() || !std::isfinite(first) || !std::isfinite(last) || first == last)
        return false;
    const gp_Pnt2d uv = pcurve->Value(first + fraction * (last - first));
    const auto surface = BRep_Tool::Surface(face);
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

// +1 / -1: the edge's curve parameter runs with / against the direction in which `face`'s
// wire traverses it. 0 when the face does not traverse the edge exactly once as a boundary
// (seam, INTERNAL/EXTERNAL imprint, or not part of the face).
int traversal_sign(const TopoDS_Face& face, const TopoDS_Edge& edge) {
    int sign = 0, uses = 0;
    for (TopExp_Explorer it(face, TopAbs_EDGE); it.More(); it.Next()) {
        if (!it.Current().IsSame(edge)) continue;
        ++uses;
        switch (it.Current().Orientation()) {
            case TopAbs_FORWARD: sign = 1; break;
            case TopAbs_REVERSED: sign = -1; break;
            default: return 0;
        }
    }
    return uses == 1 ? sign : 0;
}

const char* curve_name(GeomAbs_CurveType t) {
    switch (t) {
        case GeomAbs_Line: return "line";
        case GeomAbs_Circle: return "circle";
        case GeomAbs_Ellipse: return "ellipse";
        case GeomAbs_BSplineCurve:
        case GeomAbs_BezierCurve: return "bspline";
        default: return "other";
    }
}

struct SampleClass {
    double theta_deg = 0;  // turning angle between the outward normals, [0, 180]
    int sign = 0;          // +1 convex, -1 concave, 0 undetermined (tangent)
    bool disagree = false; // the two faces' traversals gave opposite signs
};

struct FaceBlend {
    bool is_blend_surface = false;
    bool concave = false;
    const char* surf = "other";
    double radius = 0;
    double arc_deg = -1;
    json extra = json::object();
};

// Classify one cylinder / torus / sphere face as concave or convex (outward normal toward
// its axis / tube circle / centre = concave). Other surface types are not blends.
FaceBlend classify_blend_surface(const TopoDS_Face& face) {
    FaceBlend out;
    const BRepAdaptor_Surface surf(face);
    const GeomAbs_SurfaceType type = surf.GetType();
    if (type != GeomAbs_Cylinder && type != GeomAbs_Torus && type != GeomAbs_Sphere) return out;

    Standard_Real u0 = 0, u1 = 0, v0 = 0, v1 = 0;
    BRepTools::UVBounds(face, u0, u1, v0, v1);
    if (!std::isfinite(u0) || !std::isfinite(u1) || !std::isfinite(v0) || !std::isfinite(v1))
        return out;
    gp_Pnt p;
    gp_Vec du, dv;
    surf.D1(0.5 * (u0 + u1), 0.5 * (v0 + v1), p, du, dv);
    gp_Vec normal = du.Crossed(dv);
    if (normal.SquareMagnitude() < 1e-24) return out;
    if (face.Orientation() == TopAbs_REVERSED) normal.Reverse();
    normal.Normalize();

    if (type == GeomAbs_Cylinder) {
        const gp_Cylinder cyl = surf.Cylinder();
        const gp_Ax1 axis = cyl.Axis();
        const gp_Vec dir(axis.Direction());
        gp_Vec radial(axis.Location(), p);
        radial.Subtract(dir.Multiplied(radial.Dot(dir)));
        out.is_blend_surface = true;
        out.concave = normal.Dot(radial) < 0;
        out.surf = "cylinder";
        out.radius = cyl.Radius();
        out.arc_deg = std::min(360.0, (u1 - u0) * kRadToDeg);
        out.extra["axis"] = vec3(axis.Direction());
        out.extra["axis_point"] = vec3(axis.Location());
    } else if (type == GeomAbs_Torus) {
        const gp_Torus tor = surf.Torus();
        const gp_Ax3 pos = tor.Position();
        const gp_Vec axis_dir(pos.Direction());
        gp_Vec in_plane(pos.Location(), p);
        in_plane.Subtract(axis_dir.Multiplied(in_plane.Dot(axis_dir)));
        const double m = in_plane.Magnitude();
        if (m < 1e-12) return out;
        in_plane.Multiply(tor.MajorRadius() / m);
        const gp_Pnt tube_center = pos.Location().Translated(in_plane);
        const gp_Vec radial(tube_center, p);
        out.is_blend_surface = true;
        out.concave = normal.Dot(radial) < 0;
        out.surf = "torus";
        out.radius = tor.MinorRadius();
        out.arc_deg = std::min(360.0, (v1 - v0) * kRadToDeg);
        out.extra["axis"] = vec3(pos.Direction());
        out.extra["axis_point"] = vec3(pos.Location());
        out.extra["major_radius_mm"] = r6(tor.MajorRadius());
    } else {
        const gp_Sphere sph = surf.Sphere();
        const gp_Vec radial(sph.Location(), p);
        out.is_blend_surface = true;
        out.concave = normal.Dot(radial) < 0;
        out.surf = "other";
        out.radius = sph.Radius();
        out.extra["axis_point"] = vec3(sph.Location());
        out.extra["surface_type"] = "sphere";
    }
    return out;
}
// Deliberately conservative: a bore must be a complete, finite cylindrical face.
// Partial cylinders (including split bores) are not assumed to be holes.
json ligament_analysis(const TopoDS_Shape& shape, const TopTools_IndexedMapOfShape& faces) {
    struct Bore {
        int face_id;
        TopoDS_Face face;
        gp_Cylinder cylinder;
        TopTools_IndexedMapOfShape edges;
    };
    constexpr std::size_t kMaxRows = 2000;
    constexpr int kMaxAttempts = 100000;
    json result = {{"evaluated", true}, {"rows", json::array()}, {"bore_count", 0}, {"total_rows", 0},
                   {"excluded_partial_cylinders", 0}, {"failed_distances", 0},
                   {"unavailable_boundary_count", 0}, {"distance_attempts", 0},
                   {"attempt_limit_reached", false}, {"truncated", false}, {"incomplete", false}};
    TopTools_IndexedMapOfShape solids;
    TopExp::MapShapes(shape, TopAbs_SOLID, solids);
    if (solids.IsEmpty()) {
        result["incomplete"] = true;
        result["reason"] = "no_solid";
        return result;
    }
    int attempts = 0;
    bool budget_exhausted = false;
    auto append = [&](json row) {
        result["total_rows"] = result["total_rows"].get<int>() + 1;
        if (result["rows"].size() < kMaxRows) result["rows"].push_back(std::move(row));
        else result["truncated"] = true;
    };
    auto failed = [&]() {
        result["failed_distances"] = result["failed_distances"].get<int>() + 1;
        result["incomplete"] = true;
    };
    for (int si = 1; si <= solids.Extent() && !budget_exhausted; ++si) {
        TopTools_IndexedMapOfShape solid_faces;
        TopExp::MapShapes(solids.FindKey(si), TopAbs_FACE, solid_faces);
        std::vector<Bore> bores;
        std::set<int> bore_ids;
        for (int i = 1; i <= solid_faces.Extent(); ++i) {
            const TopoDS_Face face = TopoDS::Face(solid_faces.FindKey(i));
            try {
                const BRepAdaptor_Surface surface(face);
                if (surface.GetType() != GeomAbs_Cylinder) continue;
                const FaceBlend fb = classify_blend_surface(face);
                if (!fb.is_blend_surface) { result["incomplete"] = true; continue; }
                if (!fb.concave) continue;
                Standard_Real u0, u1, v0, v1;
                BRepTools::UVBounds(face, u0, u1, v0, v1);
                const gp_Cylinder cylinder = surface.Cylinder();
                GProp_GProps props;
                BRepGProp::SurfaceProperties(face, props);
                const double expected_area = 2.0 * kPi * cylinder.Radius() * (v1 - v0);
                if (!std::isfinite(u0) || !std::isfinite(u1) || !std::isfinite(v0) ||
                    !std::isfinite(v1) || v1 <= v0 ||
                    std::abs((u1 - u0) - 2.0 * kPi) > 1e-6 ||
                    !std::isfinite(expected_area) || expected_area <= 0 ||
                    std::abs(props.Mass() - expected_area) > expected_area * 1e-6) {
                    result["excluded_partial_cylinders"] =
                        result["excluded_partial_cylinders"].get<int>() + 1;
                    result["incomplete"] = true;
                    continue;
                }
                const int face_id = faces.FindIndex(face);
                if (face_id < 1) { result["incomplete"] = true; continue; }
                Bore bore{face_id, face, cylinder, {}};
                TopExp::MapShapes(face, TopAbs_EDGE, bore.edges);
                bores.push_back(std::move(bore));
                bore_ids.insert(face_id);
            } catch (const Standard_Failure&) {
                result["incomplete"] = true;
            }
        }
        result["bore_count"] = result["bore_count"].get<int>() + static_cast<int>(bores.size());
        auto incident = [](const Bore& bore, const TopoDS_Face& face) {
            for (TopExp_Explorer ex(face, TopAbs_EDGE); ex.More(); ex.Next())
                if (bore.edges.Contains(ex.Current())) return true;
            return false;
        };
        auto measure = [&](const Bore& bore, const TopoDS_Face& other, double& distance,
                           gp_Pnt& point, gp_Pnt& other_point) {
            if (attempts >= kMaxAttempts) {
                budget_exhausted = true;
                result["incomplete"] = true;
                return false;
            }
            ++attempts;
            try {
                BRepExtrema_DistShapeShape extrema(bore.face, other);
                if (!extrema.IsDone() || extrema.NbSolution() < 1 ||
                    !std::isfinite(extrema.Value()) || extrema.Value() <= 1e-7) {
                    failed();
                    return false;
                }
                distance = extrema.Value();
                point = extrema.PointOnShape1(1);
                other_point = extrema.PointOnShape2(1);
                return true;
            } catch (const Standard_Failure&) {
                failed();
                return false;
            }
        };
        for (const Bore& bore : bores) {
            bool have_best = false, complete = true;
            int best_id = 0;
            double best = 0;
            gp_Pnt best_point, best_other;
            for (int i = 1; i <= solid_faces.Extent(); ++i) {
                const TopoDS_Face other = TopoDS::Face(solid_faces.FindKey(i));
                const int other_id = faces.FindIndex(other);
                if (other_id < 1) { complete = false; result["incomplete"] = true; continue; }
                if (other_id == bore.face_id || bore_ids.count(other_id) || incident(bore, other)) continue;
                double distance;
                gp_Pnt point, other_point;
                if (!measure(bore, other, distance, point, other_point)) {
                    complete = false;
                    if (budget_exhausted) break;
                    continue;
                }
                if (!have_best || distance < best) {
                    have_best = true; best = distance; best_id = other_id;
                    best_point = point; best_other = other_point;
                }
            }
            if (!complete || !have_best) {
                result["unavailable_boundary_count"] =
                    result["unavailable_boundary_count"].get<int>() + 1;
                result["incomplete"] = true;
            }
            if (complete && have_best)
                append({{"kind", "bore_boundary"}, {"face_id", bore.face_id},
                        {"other_face_id", best_id}, {"radius_mm", bore.cylinder.Radius()},
                        {"distance_mm", best},
                        {"point", json::array({best_point.X(), best_point.Y(), best_point.Z()})},
                        {"other_point", json::array({best_other.X(), best_other.Y(), best_other.Z()})},
                        {"basis", "minimum_distance_between_finite_faces"}});
            if (budget_exhausted) break;
        }
        for (std::size_t i = 0; i < bores.size() && !budget_exhausted; ++i) {
            for (std::size_t j = i + 1; j < bores.size(); ++j) {
                const Bore& a = bores[i];
                const Bore& b = bores[j];
                if (incident(a, b.face)) continue;
                const gp_Ax1 ax = a.cylinder.Axis(), bx = b.cylinder.Axis();
                if (ax.Direction().IsParallel(bx.Direction(), 1e-7) &&
                    gp_Vec(ax.Location(), bx.Location()).Crossed(gp_Vec(ax.Direction())).Magnitude() <= 1e-7) {
                    // Coaxial cylindrical faces can be steps of the same hole.
                    result["incomplete"] = true;
                    continue;
                }
                double distance;
                gp_Pnt point, other_point;
                if (measure(a, b.face, distance, point, other_point))
                    append({{"kind", "bore_bore"}, {"face_id", a.face_id},
                            {"other_face_id", b.face_id}, {"radius_mm", a.cylinder.Radius()},
                            {"other_radius_mm", b.cylinder.Radius()}, {"distance_mm", distance},
                            {"point", json::array({point.X(), point.Y(), point.Z()})},
                            {"other_point", json::array({other_point.X(), other_point.Y(), other_point.Z()})},
                            {"basis", "minimum_distance_between_finite_faces"}});
                if (budget_exhausted) break;
            }
        }
    }
    result["distance_attempts"] = attempts;
    result["attempt_limit_reached"] = budget_exhausted;
    if (result["truncated"].get<bool>()) result["incomplete"] = true;
    return result;
}


double option_number(const json& options, const char* key, double fallback, double lo, double hi) {
    if (!options.contains(key) || options[key].is_null()) return fallback;
    if (!options[key].is_number())
        throw std::invalid_argument(std::string(key) + " must be numeric");
    const double v = options[key].get<double>();
    if (!std::isfinite(v) || v < lo || v >= hi)
        throw std::invalid_argument(std::string(key) + " must be in [" + std::to_string(lo) +
                                    ", " + std::to_string(hi) + ")");
    return v;
}

} // namespace

json edge_analysis(const TopoDS_Shape& shape, const json& options) {
    if (!options.is_object()) throw std::invalid_argument("edge analysis expects an options object");
    if (shape.IsNull()) throw std::invalid_argument("edge analysis needs a non-null shape");
    const double min_dev = option_number(options, "min_dihedral_dev_deg", 5.0, 0.0, 90.0);
    bool include_ligaments = false;
    if (options.contains("include_ligaments")) {
        if (!options["include_ligaments"].is_boolean())
            throw std::invalid_argument("include_ligaments must be boolean");
        include_ligaments = options["include_ligaments"].get<bool>();
    }

    TopTools_IndexedMapOfShape faces, edges;
    TopExp::MapShapes(shape, TopAbs_FACE, faces);
    TopExp::MapShapes(shape, TopAbs_EDGE, edges);
    TopTools_IndexedDataMapOfShapeListOfShape ancestors;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, ancestors);

    json rows = json::array();
    json unclassified = json::array();
    int seam_or_degenerate = 0, non_manifold_or_free = 0;
    std::vector<std::set<int>> adjacent(static_cast<size_t>(faces.Extent()) + 1);
    std::vector<std::set<int>> tangent(static_cast<size_t>(faces.Extent()) + 1);

    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges.FindKey(i));
        if (BRep_Tool::Degenerated(edge) || !ancestors.Contains(edge)) {
            ++seam_or_degenerate;
            continue;
        }
        const TopTools_ListOfShape& listed = ancestors.FindFromKey(edge);
        std::vector<TopoDS_Face> distinct;
        for (TopTools_ListIteratorOfListOfShape it(listed); it.More(); it.Next()) {
            const TopoDS_Face f = TopoDS::Face(it.Value());
            const bool seen = std::any_of(distinct.begin(), distinct.end(),
                                          [&](const TopoDS_Face& g) { return g.IsSame(f); });
            if (!seen) distinct.push_back(f);
        }
        if (distinct.size() == 1 && listed.Extent() >= 2) { ++seam_or_degenerate; continue; }
        if (distinct.size() != 2) { ++non_manifold_or_free; continue; }

        const TopoDS_Face& face_a = distinct[0];
        const TopoDS_Face& face_b = distinct[1];
        const int ia = faces.FindIndex(face_a), ib = faces.FindIndex(face_b);
        if (ia < 1 || ib < 1) { ++non_manifold_or_free; continue; }
        const int sign_a = traversal_sign(face_a, edge), sign_b = traversal_sign(face_b, edge);
        if (sign_a == 0 || sign_b == 0 || sign_a == sign_b) {
            unclassified.push_back({{"edge_id", i}, {"reason", "inconsistent_face_orientation"}});
            continue;
        }

        try {
            BRepAdaptor_Curve curve(edge);
            const double f = curve.FirstParameter(), l = curve.LastParameter();
            if (!std::isfinite(f) || !std::isfinite(l) || f == l) {
                unclassified.push_back({{"edge_id", i}, {"reason", "unbounded_edge"}});
                continue;
            }
            std::vector<SampleClass> samples;
            for (const double t : kSamples) {
                gp_Vec na, nb;
                if (!outward_normal(edge, face_a, t, na) || !outward_normal(edge, face_b, t, nb))
                    continue;
                gp_Pnt p;
                gp_Vec d1;
                curve.D1(f + t * (l - f), p, d1);
                if (d1.SquareMagnitude() < 1e-24) continue;
                d1.Normalize();
                const gp_Vec cross = na.Crossed(nb);
                SampleClass sc;
                sc.theta_deg = std::atan2(cross.Magnitude(), na.Dot(nb)) * kRadToDeg;
                // Face A traverses the edge along sign_a*d1, face B along sign_b*d1; the
                // cross product of the outward normals is parallel to the tangent, so
                // each traversal yields +-sin(theta). They must agree.
                const double sa = d1.Multiplied(sign_a).Dot(cross);
                const double sb = -d1.Multiplied(sign_b).Dot(cross);
                constexpr double kTiny = 1e-9;
                if (std::abs(sa) > kTiny && std::abs(sb) > kTiny) {
                    if ((sa > 0) == (sb > 0)) sc.sign = sa > 0 ? 1 : -1;
                    else sc.disagree = true;
                } else if (std::abs(sa) > kTiny || std::abs(sb) > kTiny) {
                    sc.sign = (std::abs(sa) > kTiny ? sa : sb) > 0 ? 1 : -1;
                }
                samples.push_back(sc);
            }
            if (samples.empty()) {
                unclassified.push_back({{"edge_id", i}, {"reason", "no_sampleable_normals"}});
                continue;
            }
            const auto widest = std::max_element(
                samples.begin(), samples.end(),
                [](const SampleClass& a, const SampleClass& b) { return a.theta_deg < b.theta_deg; });
            const auto narrowest = std::min_element(
                samples.begin(), samples.end(),
                [](const SampleClass& a, const SampleClass& b) { return a.theta_deg < b.theta_deg; });
            // A zero threshold must still recognize exactly tangent faces.
            const bool deviates = widest->theta_deg > std::max(min_dev, 1e-7);
            if (deviates && (widest->disagree || widest->sign == 0)) {
                unclassified.push_back({{"edge_id", i}, {"reason", "face_traversals_disagree"}});
                continue;
            }
            bool mixed = false;
            for (const SampleClass& sc : samples)
                if (sc.theta_deg >= min_dev && sc.sign != 0 && sc.sign != widest->sign) mixed = true;

            const char* kind = !deviates ? "smooth" : (widest->sign > 0 ? "convex" : "concave");
            // Interior (material-side) angle: convex = 180 - theta, concave = 180 + theta.
            const double dihedral = widest->sign > 0 ? 180.0 - widest->theta_deg
                                  : widest->sign < 0 ? 180.0 + widest->theta_deg : 180.0;

            adjacent[static_cast<size_t>(ia)].insert(ib);
            adjacent[static_cast<size_t>(ib)].insert(ia);
            if (!deviates) {
                tangent[static_cast<size_t>(ia)].insert(ib);
                tangent[static_cast<size_t>(ib)].insert(ia);
            }

            gp_Pnt mid;
            gp_Vec mid_dir;
            curve.D1(f + 0.5 * (l - f), mid, mid_dir);
            if (mid_dir.SquareMagnitude() > 1e-24) mid_dir.Normalize();
            GProp_GProps props;
            BRepGProp::LinearProperties(edge, props);

            json row = {{"edge_id", i},
                        {"kind", kind},
                        {"dihedral_deg", r6(dihedral)},
                        {"deviation_deg", r6(widest->theta_deg)},
                        {"deviation_min_deg", r6(narrowest->theta_deg)},
                        {"length_mm", r6(props.Mass())},
                        {"mid", vec3(mid)},
                        {"dir", vec3(mid_dir)},
                        {"face_ids", json::array({ia, ib})},
                        {"curve", curve_name(curve.GetType())}};
            if (curve.GetType() == GeomAbs_Circle) {
                const gp_Circ c = curve.Circle();
                row["radius_mm"] = r6(c.Radius());
                row["axis"] = vec3(c.Axis().Direction());
            }
            if (mixed) row["mixed_sign"] = true;
            rows.push_back(std::move(row));
        } catch (const Standard_Failure&) {
            unclassified.push_back({{"edge_id", i}, {"reason", "kernel_failure"}});
        }
    }

    json blends = json::array();
    for (int fi = 1; fi <= faces.Extent(); ++fi) {
        if (tangent[static_cast<size_t>(fi)].empty()) continue;  // a hole or bore, not a blend
        const TopoDS_Face face = TopoDS::Face(faces.FindKey(fi));
        try {
            const FaceBlend fb = classify_blend_surface(face);
            if (!fb.is_blend_surface || !fb.concave) continue;
            GProp_GProps props;
            BRepGProp::SurfaceProperties(face, props);
            json row = {{"face_id", fi},
                        {"surf", fb.surf},
                        {"radius_mm", r6(fb.radius)},
                        {"center", vec3(props.CentreOfMass())},
                        {"area_mm2", r6(props.Mass())},
                        {"adjacent_face_ids", json(adjacent[static_cast<size_t>(fi)])},
                        {"tangent_face_ids", json(tangent[static_cast<size_t>(fi)])}};
            if (fb.arc_deg >= 0) row["arc_deg"] = r6(fb.arc_deg);
            for (auto it = fb.extra.begin(); it != fb.extra.end(); ++it) row[it.key()] = it.value();
            blends.push_back(std::move(row));
        } catch (const Standard_Failure&) {
            // An unreadable face is simply not reported as a blend.
        }
    }

    return {{"face_count", faces.Extent()},
            {"edge_count", edges.Extent()},
            {"min_dihedral_dev_deg", min_dev},
            {"edges", std::move(rows)},
            {"concave_blends", std::move(blends)},
            {"ligaments", include_ligaments ? ligament_analysis(shape, faces) : json{{"evaluated", false}}},
            {"unclassified_edges", std::move(unclassified)},
            {"skipped_edges", {{"seam_or_degenerate", seam_or_degenerate},
                               {"non_manifold_or_free", non_manifold_or_free}}}};
}

} // namespace chudware::geom
