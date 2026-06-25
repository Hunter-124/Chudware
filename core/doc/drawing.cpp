// 2D drawing projection via OCCT HLR — see drawing.h. Port of engine/doc/drawing.py.

#include "doc/drawing.h"

#include <array>
#include <cmath>
#include <stdexcept>

#include <BRep_Builder.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepTools.hxx>
#include <GCPnts_TangentialDeflection.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <HLRAlgo_Projector.hxx>
#include <HLRBRep_Algo.hxx>
#include <HLRBRep_HLRToShape.hxx>
#include <Standard_Handle.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

using nlohmann::json;

namespace aicad::doc {

namespace {

#ifndef AICAD_PI
#define AICAD_PI 3.14159265358979323846
#endif

using Vec3 = std::array<double, 3>;

Vec3 normalize(Vec3 v) {
    double n = std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
    if (n < 1e-30) n = 1.0;
    return {v[0] / n, v[1] / n, v[2] / n};
}

Vec3 cross(const Vec3& a, const Vec3& b) {
    return {a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]};
}

double dot(const Vec3& a, const Vec3& b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

// orientation -> (view_dir, screen_x). Mirrors drawing.py::_ORIENT + _iso_axes.
bool orient_axes(const std::string& orientation, Vec3& view_dir, Vec3& screen_x) {
    if (orientation == "front") {
        view_dir = {0, 1, 0};
        screen_x = {1, 0, 0};
    } else if (orientation == "back") {
        view_dir = {0, -1, 0};
        screen_x = {-1, 0, 0};
    } else if (orientation == "top") {
        view_dir = {0, 0, -1};
        screen_x = {1, 0, 0};
    } else if (orientation == "bottom") {
        view_dir = {0, 0, 1};
        screen_x = {1, 0, 0};
    } else if (orientation == "right") {
        view_dir = {-1, 0, 0};
        screen_x = {0, 1, 0};
    } else if (orientation == "left") {
        view_dir = {1, 0, 0};
        screen_x = {0, -1, 0};
    } else if (orientation == "iso") {
        // Eye at (1,1,1) -> view dir = -(1,1,1) normalized; screen_x = up x dir.
        Vec3 d = normalize({-1, -1, -1});
        Vec3 up = {0, 0, 1};
        Vec3 sx = normalize(cross(up, d));
        view_dir = d;
        screen_x = sx;
    } else {
        return false;
    }
    return true;
}

// Discretize one projected (2D, z~0) edge into a list of [x,y] sheet points.
// Mirrors _edge_to_polyline: lines stay 2-point, curves use GCPnts_TangentialDeflection.
std::vector<std::array<double, 2>> edge_to_polyline(const TopoDS_Edge& edge,
                                                    double lin_defl, double ang_defl) {
    std::vector<std::array<double, 2>> pts;
    BRepAdaptor_Curve ad(edge);
    if (ad.GetType() == GeomAbs_Line) {
        gp_Pnt a = ad.Value(ad.FirstParameter());
        gp_Pnt b = ad.Value(ad.LastParameter());
        pts.push_back({a.X(), a.Y()});
        pts.push_back({b.X(), b.Y()});
    } else {
        try {
            GCPnts_TangentialDeflection disc(ad, lin_defl, ang_defl);
            for (int i = 1; i <= disc.NbPoints(); ++i) {
                gp_Pnt p = disc.Value(i);
                pts.push_back({p.X(), p.Y()});
            }
        } catch (...) {
            gp_Pnt a = ad.Value(ad.FirstParameter());
            gp_Pnt b = ad.Value(ad.LastParameter());
            pts.push_back({a.X(), a.Y()});
            pts.push_back({b.X(), b.Y()});
        }
    }
    return pts;
}

// Every edge in an HLR result compound -> scaled, positioned 2D polylines.
// Mirrors _compound_polylines. A null/empty compound yields none.
void compound_polylines(const TopoDS_Shape& compound, double scale,
                        std::array<double, 2> position, double lin_defl,
                        double ang_defl,
                        std::vector<std::vector<std::array<double, 2>>>& out) {
    if (compound.IsNull()) return;
    TopTools_IndexedMapOfShape emap;
    TopExp::MapShapes(compound, TopAbs_EDGE, emap);
    double px = position[0], py = position[1];
    for (int i = 1; i <= emap.Extent(); ++i) {
        TopoDS_Edge edge = TopoDS::Edge(emap.FindKey(i));
        double s = scale > 1e-6 ? scale : 1e-6;
        auto raw = edge_to_polyline(edge, lin_defl / s, ang_defl);
        if (raw.size() < 2) continue;
        std::vector<std::array<double, 2>> pl;
        pl.reserve(raw.size());
        for (const auto& p : raw)
            pl.push_back({p[0] * scale + px, p[1] * scale + py});
        out.push_back(std::move(pl));
    }
}

// Every circular-arc edge -> an analytic arc entity. Mirrors _compound_arcs.
void compound_arcs(const TopoDS_Shape& compound, double scale,
                   std::array<double, 2> position, const std::string& layer,
                   json& out) {
    if (compound.IsNull()) return;
    TopTools_IndexedMapOfShape emap;
    TopExp::MapShapes(compound, TopAbs_EDGE, emap);
    double px = position[0], py = position[1];
    for (int i = 1; i <= emap.Extent(); ++i) {
        TopoDS_Edge edge = TopoDS::Edge(emap.FindKey(i));
        double r, fp, lp, cx0, cy0;
        try {
            BRepAdaptor_Curve ad(edge);
            if (ad.GetType() != GeomAbs_Circle) continue;
            gp_Circ circ = ad.Circle();
            gp_Pnt loc = circ.Location();
            r = circ.Radius();
            fp = ad.FirstParameter();
            lp = ad.LastParameter();
            cx0 = loc.X();
            cy0 = loc.Y();
        } catch (...) {
            continue;
        }
        double span = lp - fp;
        double cx = cx0 * scale + px;
        double cy = cy0 * scale + py;
        out.push_back(json{
            {"center", json::array({cx, cy})},
            {"radius", r * scale},
            {"start_angle", fp},
            {"end_angle", lp},
            {"span", span},
            {"closed", std::fabs(std::fabs(span) - 2.0 * AICAD_PI) < 1e-6},
            {"layer", layer},
        });
    }
}

// bbox over all polyline points (+ extra polygon lists). Null when empty.
json bbox_of(const std::vector<std::vector<std::array<double, 2>>>& a,
             const std::vector<std::vector<std::array<double, 2>>>& b) {
    bool any = false;
    double minx = 0, miny = 0, maxx = 0, maxy = 0;
    auto scan = [&](const std::vector<std::vector<std::array<double, 2>>>& pls) {
        for (const auto& pl : pls)
            for (const auto& p : pl) {
                if (!any) {
                    minx = maxx = p[0];
                    miny = maxy = p[1];
                    any = true;
                } else {
                    minx = std::min(minx, p[0]);
                    miny = std::min(miny, p[1]);
                    maxx = std::max(maxx, p[0]);
                    maxy = std::max(maxy, p[1]);
                }
            }
    };
    scan(a);
    scan(b);
    if (!any) return json(nullptr);
    return json::array({minx, miny, maxx, maxy});
}

json polylines_to_json(const std::vector<std::vector<std::array<double, 2>>>& pls) {
    json arr = json::array();
    for (const auto& pl : pls) {
        json j = json::array();
        for (const auto& p : pl) j.push_back(json::array({p[0], p[1]}));
        arr.push_back(std::move(j));
    }
    return arr;
}

}  // namespace

json project_view(const std::vector<TopoDS_Shape>& shapes,
                  const std::string& orientation, double scale,
                  std::array<double, 2> position, bool show_hidden,
                  const std::string& view_id) {
    json base = {{"view_id", view_id}, {"scale", scale},
                 {"orientation", orientation}, {"visible", json::array()},
                 {"hidden", json::array()}, {"arcs", json::array()},
                 {"bbox", json(nullptr)}, {"ms", 0.0}, {"error", json(nullptr)}};
    if (shapes.empty()) {
        base["error"] = "source has no bodies to project";
        return base;
    }
    try {
        Vec3 view_dir, screen_x;
        if (!orient_axes(orientation, view_dir, screen_x)) {
            base["error"] = "unknown orientation '" + orientation + "'";
            return base;
        }
        gp_Ax2 ax(gp_Pnt(0, 0, 0),
                  gp_Dir(view_dir[0], view_dir[1], view_dir[2]),
                  gp_Dir(screen_x[0], screen_x[1], screen_x[2]));
        HLRAlgo_Projector projector(ax);

        Handle(HLRBRep_Algo) algo = new HLRBRep_Algo();
        for (const auto& sh : shapes) algo->Add(sh);
        algo->Projector(projector);
        algo->Update();
        algo->Hide();
        HLRBRep_HLRToShape hlr(algo);

        const double lin_defl = 0.05, ang_defl = 0.35;
        std::vector<std::vector<std::array<double, 2>>> visible, hidden;
        compound_polylines(hlr.VCompound(), scale, position, lin_defl, ang_defl, visible);
        compound_polylines(hlr.OutLineVCompound(), scale, position, lin_defl, ang_defl, visible);
        compound_polylines(hlr.Rg1LineVCompound(), scale, position, lin_defl, ang_defl, visible);
        if (show_hidden) {
            compound_polylines(hlr.HCompound(), scale, position, lin_defl, ang_defl, hidden);
            compound_polylines(hlr.OutLineHCompound(), scale, position, lin_defl, ang_defl, hidden);
        }

        json arcs = json::array();
        compound_arcs(hlr.VCompound(), scale, position, "visible", arcs);
        compound_arcs(hlr.OutLineVCompound(), scale, position, "visible", arcs);
        if (show_hidden) {
            compound_arcs(hlr.HCompound(), scale, position, "hidden", arcs);
            compound_arcs(hlr.OutLineHCompound(), scale, position, "hidden", arcs);
        }

        base["visible"] = polylines_to_json(visible);
        base["hidden"] = polylines_to_json(hidden);
        base["arcs"] = arcs;
        base["bbox"] = bbox_of(visible, hidden);
    } catch (const std::exception& exc) {
        base["error"] = std::string("exception: ") + exc.what();
    } catch (...) {
        base["error"] = "unknown HLR exception";
    }
    return base;
}

namespace {

// A half-space box: the region on the +normal side of the plane through `point`, extending
// `depth` along +normal (depth<=0 -> BIG, a true half-space). Returned as a TopoDS box tool.
TopoDS_Shape half_space_box(Vec3 normal, Vec3 point, double depth) {
    Vec3 n = normalize(normal);
    const double BIG = 100000.0;
    double dz = (depth > 0.0) ? depth : BIG;
    Vec3 seed = {1, 0, 0};
    if (std::fabs(dot(seed, n)) > 0.9) seed = {0, 1, 0};
    Vec3 u = normalize(cross(n, seed));
    Vec3 nu = cross(n, u);
    Vec3 origin = {point[0] - u[0] * BIG - nu[0] * BIG,
                   point[1] - u[1] * BIG - nu[1] * BIG,
                   point[2] - u[2] * BIG - nu[2] * BIG};
    gp_Ax2 ax(gp_Pnt(origin[0], origin[1], origin[2]),
              gp_Dir(n[0], n[1], n[2]), gp_Dir(u[0], u[1], u[2]));
    return BRepPrimAPI_MakeBox(ax, 2 * BIG, 2 * BIG, dz).Shape();
}

// Cut raw_shape by the +normal half-space through point. Mirrors _half_space_cut.
TopoDS_Shape half_space_cut(const TopoDS_Shape& raw, Vec3 normal, Vec3 point) {
    TopoDS_Shape tool = half_space_box(normal, point, 0.0);
    BRepAlgoAPI_Cut cut(raw, tool);
    cut.Build();
    if (!cut.IsDone()) throw std::runtime_error("section half-space cut failed");
    return cut.Shape();
}

// Section cut for a view: removes the +normal side of the plane (mode "full"), and for a
// HALF section removes only the half on the +half_axis side (intersection of the two
// half-spaces) so the other half stays an unsectioned external view. `depth` (>0) limits
// how deep behind the plane is removed. Returns the cut body (or the original on failure).
TopoDS_Shape section_cut(const TopoDS_Shape& raw, Vec3 normal, Vec3 point, const std::string& mode,
                         double depth, const Vec3* half_axis) {
    TopoDS_Shape tool = half_space_box(normal, point, depth);
    if (mode == "half" && half_axis && std::sqrt(dot(*half_axis, *half_axis)) > 1e-9) {
        TopoDS_Shape side = half_space_box(*half_axis, point, 0.0);
        BRepAlgoAPI_Common common(tool, side);
        common.Build();
        if (common.IsDone()) tool = common.Shape();
    }
    BRepAlgoAPI_Cut cut(raw, tool);
    cut.Build();
    if (!cut.IsDone()) throw std::runtime_error("section cut failed");
    return cut.Shape();
}

// Cut-plane face outer-wire polygons projected to (screen_x, screen_y). Mirrors
// _section_face_polygons.
void section_face_polygons(const TopoDS_Shape& cut_shape, Vec3 normal, Vec3 point,
                           Vec3 sx, Vec3 sy, double scale,
                           std::array<double, 2> position,
                           std::vector<std::vector<std::array<double, 2>>>& polys) {
    Vec3 n = normalize(normal);
    double px = position[0], py = position[1];
    TopTools_IndexedMapOfShape fmap;
    TopExp::MapShapes(cut_shape, TopAbs_FACE, fmap);
    for (int i = 1; i <= fmap.Extent(); ++i) {
        TopoDS_Face f = TopoDS::Face(fmap.FindKey(i));
        BRepAdaptor_Surface surf(f);
        if (surf.GetType() != GeomAbs_Plane) continue;
        GProp_GProps g;
        BRepGProp::SurfaceProperties(f, g);
        gp_Pnt c = g.CentreOfMass();
        Vec3 cvec = {c.X(), c.Y(), c.Z()};
        gp_Dir fn = surf.Plane().Axis().Direction();
        Vec3 fnv = {fn.X(), fn.Y(), fn.Z()};
        Vec3 cp = {cvec[0] - point[0], cvec[1] - point[1], cvec[2] - point[2]};
        if (std::fabs(dot(cp, n)) > 1e-6) continue;
        if (std::fabs(std::fabs(dot(fnv, n)) - 1.0) > 1e-6) continue;
        TopoDS_Wire ow;
        try {
            ow = BRepTools::OuterWire(f);
        } catch (...) {
            continue;
        }
        std::vector<std::array<double, 2>> poly;
        for (TopExp_Explorer exp(ow, TopAbs_EDGE); exp.More(); exp.Next()) {
            TopoDS_Edge e = TopoDS::Edge(exp.Current());
            BRepAdaptor_Curve ad(e);
            std::vector<gp_Pnt> pts3;
            if (ad.GetType() == GeomAbs_Line) {
                pts3.push_back(ad.Value(ad.FirstParameter()));
                pts3.push_back(ad.Value(ad.LastParameter()));
            } else {
                try {
                    double s = scale > 1e-6 ? scale : 1e-6;
                    GCPnts_TangentialDeflection disc(ad, 0.05 / s, 0.3);
                    for (int k = 1; k <= disc.NbPoints(); ++k) pts3.push_back(disc.Value(k));
                } catch (...) {
                    pts3.push_back(ad.Value(ad.FirstParameter()));
                    pts3.push_back(ad.Value(ad.LastParameter()));
                }
            }
            for (const gp_Pnt& q : pts3) {
                Vec3 v = {q.X(), q.Y(), q.Z()};
                poly.push_back({dot(v, sx) * scale + px, dot(v, sy) * scale + py});
            }
        }
        if (poly.size() >= 3) polys.push_back(std::move(poly));
    }
}

}  // namespace

json project_section(const std::vector<TopoDS_Shape>& shapes, const json& plane,
                     double scale, std::array<double, 2> position, bool show_hidden,
                     const std::string& view_id) {
    json base = {{"view_id", view_id}, {"scale", scale}, {"orientation", "section"},
                 {"visible", json::array()}, {"hidden", json::array()},
                 {"section_faces", json::array()}, {"bbox", json(nullptr)},
                 {"ms", 0.0}, {"error", json(nullptr)}};
    if (shapes.empty()) {
        base["error"] = "source has no bodies to project";
        return base;
    }
    // Resolve the explicit world plane {normal, point} (cut_line form deferred).
    if (!plane.is_object() || !plane.contains("normal") || !plane.contains("point"))
        throw std::runtime_error("section needs a world 'plane' {normal, point}");
    Vec3 normal = {plane["normal"][0], plane["normal"][1], plane["normal"][2]};
    Vec3 point = {plane["point"][0], plane["point"][1], plane["point"][2]};
    if (std::sqrt(dot(normal, normal)) < 1e-9)
        throw std::runtime_error("section plane.normal must be nonzero");
    normal = normalize(normal);
    // Custom-section controls (optional): mode (full|half), depth clip, reverse side, and
    // the in-plane half_axis along which a half section keeps the unsectioned half.
    std::string mode = plane.value("mode", std::string("full"));
    double depth = plane.value("depth", 0.0);
    if (plane.value("reverse", false)) normal = {-normal[0], -normal[1], -normal[2]};
    Vec3 half_axis{0, 0, 0};
    bool have_half = false;
    if (plane.contains("half_axis") && plane["half_axis"].is_array() &&
        plane["half_axis"].size() == 3) {
        half_axis = {plane["half_axis"][0], plane["half_axis"][1], plane["half_axis"][2]};
        have_half = std::sqrt(dot(half_axis, half_axis)) > 1e-9;
    }
    Vec3 look = normal;
    // screen_x: a world axis most perpendicular to the look.
    Vec3 seed = {0, 0, 1};
    if (std::fabs(dot(seed, look)) > 0.9) seed = {1, 0, 0};
    Vec3 sx = normalize(cross(seed, look));

    try {
        // Re-orthogonalize sx against look.
        double sl = dot(sx, look);
        sx = {sx[0] - look[0] * sl, sx[1] - look[1] * sl, sx[2] - look[2] * sl};
        if (std::sqrt(dot(sx, sx)) < 1e-9) {
            Vec3 sd = {0, 0, 1};
            if (std::fabs(dot(sd, look)) > 0.9) sd = {1, 0, 0};
            sx = cross(sd, look);
        }
        sx = normalize(sx);
        Vec3 sy = normalize(cross(look, sx));

        std::vector<TopoDS_Shape> cut_shapes;
        std::vector<std::vector<std::array<double, 2>>> section_faces;
        for (const auto& raw : shapes) {
            TopoDS_Shape cs;
            try {
                cs = section_cut(raw, look, point, mode, depth, have_half ? &half_axis : nullptr);
            } catch (...) {
                cs = raw;
            }
            cut_shapes.push_back(cs);
            section_face_polygons(cs, normal, point, sx, sy, scale, position, section_faces);
        }

        gp_Ax2 ax(gp_Pnt(0, 0, 0), gp_Dir(look[0], look[1], look[2]),
                  gp_Dir(sx[0], sx[1], sx[2]));
        HLRAlgo_Projector projector(ax);
        Handle(HLRBRep_Algo) algo = new HLRBRep_Algo();
        for (const auto& cs : cut_shapes) algo->Add(cs);
        algo->Projector(projector);
        algo->Update();
        algo->Hide();
        HLRBRep_HLRToShape hlr(algo);

        const double lin_defl = 0.05, ang_defl = 0.35;
        std::vector<std::vector<std::array<double, 2>>> visible, hidden;
        compound_polylines(hlr.VCompound(), scale, position, lin_defl, ang_defl, visible);
        compound_polylines(hlr.OutLineVCompound(), scale, position, lin_defl, ang_defl, visible);
        compound_polylines(hlr.Rg1LineVCompound(), scale, position, lin_defl, ang_defl, visible);
        if (show_hidden) {
            compound_polylines(hlr.HCompound(), scale, position, lin_defl, ang_defl, hidden);
            compound_polylines(hlr.OutLineHCompound(), scale, position, lin_defl, ang_defl, hidden);
        }

        base["visible"] = polylines_to_json(visible);
        base["hidden"] = polylines_to_json(hidden);
        base["section_faces"] = polylines_to_json(section_faces);
        // bbox over visible + hidden + section_faces.
        std::vector<std::vector<std::array<double, 2>>> all = visible;
        all.insert(all.end(), hidden.begin(), hidden.end());
        all.insert(all.end(), section_faces.begin(), section_faces.end());
        base["bbox"] = bbox_of(all, {});
        if (section_faces.empty()) base["error"] = "cut plane does not intersect the model";
    } catch (const std::exception& exc) {
        base["error"] = std::string("exception: ") + exc.what();
    } catch (...) {
        base["error"] = "unknown section exception";
    }
    return base;
}

namespace {

// Clip a sheet polyline to the disc; mirrors _clip_polyline_to_circle.
std::vector<std::vector<std::array<double, 2>>> clip_to_circle(
    const std::vector<std::array<double, 2>>& pl, double cx, double cy, double r) {
    double r2 = r * r;
    auto inside = [&](const std::array<double, 2>& p) {
        return (p[0] - cx) * (p[0] - cx) + (p[1] - cy) * (p[1] - cy) <= r2;
    };
    auto intersect = [&](const std::array<double, 2>& p0,
                         const std::array<double, 2>& p1, double& t) -> bool {
        double dx = p1[0] - p0[0], dy = p1[1] - p0[1];
        double fx = p0[0] - cx, fy = p0[1] - cy;
        double a = dx * dx + dy * dy;
        if (a < 1e-12) return false;
        double b = 2 * (fx * dx + fy * dy);
        double c = fx * fx + fy * fy - r2;
        double disc = b * b - 4 * a * c;
        if (disc < 0) return false;
        double sq = std::sqrt(disc);
        double t0 = (-b - sq) / (2 * a), t1 = (-b + sq) / (2 * a);
        bool ok = false;
        double best = 2.0;
        for (double tt : {t0, t1})
            if (tt >= 0.0 && tt <= 1.0 && tt < best) {
                best = tt;
                ok = true;
            }
        if (ok) t = best;
        return ok;
    };
    std::vector<std::vector<std::array<double, 2>>> out;
    std::vector<std::array<double, 2>> cur;
    for (std::size_t i = 0; i < pl.size(); ++i) {
        const auto& pt = pl[i];
        if (inside(pt)) {
            if (cur.empty() && i > 0) {
                double t;
                if (intersect(pl[i - 1], pt, t))
                    cur.push_back({pl[i - 1][0] + t * (pt[0] - pl[i - 1][0]),
                                   pl[i - 1][1] + t * (pt[1] - pl[i - 1][1])});
            }
            cur.push_back(pt);
        } else {
            if (!cur.empty()) {
                double t;
                if (intersect(pl[i - 1], pt, t))
                    cur.push_back({pl[i - 1][0] + t * (pt[0] - pl[i - 1][0]),
                                   pl[i - 1][1] + t * (pt[1] - pl[i - 1][1])});
                if (cur.size() >= 2) out.push_back(cur);
                cur.clear();
            } else if (i > 0) {
                const auto& p0 = pl[i - 1];
                const auto& p1 = pt;
                double dx = p1[0] - p0[0], dy = p1[1] - p0[1];
                double fx = p0[0] - cx, fy = p0[1] - cy;
                double a = dx * dx + dy * dy;
                if (a >= 1e-12) {
                    double b = 2 * (fx * dx + fy * dy);
                    double cc = fx * fx + fy * fy - r2;
                    double dsc = b * b - 4 * a * cc;
                    if (dsc > 0) {
                        double sq = std::sqrt(dsc);
                        double t0 = (-b - sq) / (2 * a), t1 = (-b + sq) / (2 * a);
                        if (t0 >= 0.0 && t0 <= 1.0 && t1 >= 0.0 && t1 <= 1.0 && t1 > t0)
                            out.push_back({{p0[0] + t0 * dx, p0[1] + t0 * dy},
                                           {p0[0] + t1 * dx, p0[1] + t1 * dy}});
                    }
                }
            }
        }
    }
    if (cur.size() >= 2) out.push_back(cur);
    return out;
}

}  // namespace

json project_detail(const json& parent_geom, std::array<double, 2> center,
                    double radius, double scale, std::array<double, 2> position,
                    const std::string& view_id) {
    json base = {{"view_id", view_id}, {"scale", scale}, {"orientation", "detail"},
                 {"visible", json::array()}, {"hidden", json::array()},
                 {"bbox", json(nullptr)}, {"ms", 0.0}, {"error", json(nullptr)}};
    double cx = center[0], cy = center[1], r = radius;
    if (r <= 0) {
        base["error"] = "detail radius must be positive";
        return base;
    }
    double parent_scale = parent_geom.value("scale", 1.0);
    if (parent_scale == 0.0) parent_scale = 1.0;
    double mag = scale / parent_scale;
    double px = position[0], py = position[1];

    try {
        std::vector<std::vector<std::array<double, 2>>> vis_out, hid_out;
        for (const char* layer : {"visible", "hidden"}) {
            std::vector<std::vector<std::array<double, 2>>>& kept =
                (std::string(layer) == "visible") ? vis_out : hid_out;
            if (!parent_geom.contains(layer) || !parent_geom[layer].is_array()) continue;
            for (const auto& jpl : parent_geom[layer]) {
                std::vector<std::array<double, 2>> pl;
                for (const auto& jp : jpl) pl.push_back({jp[0], jp[1]});
                for (auto& sub : clip_to_circle(pl, cx, cy, r)) {
                    if (sub.size() < 2) continue;
                    std::vector<std::array<double, 2>> remapped;
                    for (const auto& p : sub)
                        remapped.push_back({(p[0] - cx) * mag + px, (p[1] - cy) * mag + py});
                    kept.push_back(std::move(remapped));
                }
            }
        }
        base["visible"] = polylines_to_json(vis_out);
        base["hidden"] = polylines_to_json(hid_out);
        base["bbox"] = bbox_of(vis_out, hid_out);
        if (vis_out.empty() && hid_out.empty())
            base["error"] = "detail circle encloses no parent geometry";
    } catch (const std::exception& exc) {
        base["error"] = std::string("exception: ") + exc.what();
    } catch (...) {
        base["error"] = "unknown detail exception";
    }
    return base;
}

}  // namespace aicad::doc
