// core/cam/extract.cpp — OCCT implementation of the CAM geometry extraction
// adapter (declared OCCT-free in cam/extract.h). Lives in the chudware_doc lib, which
// links the OCCT data-exchange + topology toolkits. Bridges a live B-rep part to
// the OCCT-free CAM operation generators.
#include "cam/extract.h"

#include <algorithm>
#include <cmath>
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Section.hxx>
#include <BRepBndLib.hxx>
#include <Bnd_Box.hxx>
#include <GCPnts_UniformDeflection.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Ax3.hxx>
#include <gp_Cone.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Pln.hxx>

#include "doc/formats.h"

namespace chudware {
namespace cam {

using nlohmann::json;

namespace {

// merge all imported bodies into one bbox + a representative compound
struct Loaded {
    std::vector<TopoDS_Shape> shapes;
    bool ok = false;
    std::string error;
};
Loaded load(const json& params) {
    Loaded L;
    std::string path = params.value("path", std::string());
    if (path.empty()) { L.error = "cam_extract: 'path' is required"; return L; }
    try {
        std::string fmt = doc::detect_format(path);
        auto bodies = doc::import_bodies(path, fmt);
        for (auto& b : bodies) if (!b.second.IsNull()) L.shapes.push_back(b.second);
        L.ok = !L.shapes.empty();
        if (!L.ok) L.error = "cam_extract: no solids in " + path;
    } catch (const std::exception& e) {
        L.error = std::string("cam_extract import failed: ") + e.what();
    }
    return L;
}

void bbox_of(const std::vector<TopoDS_Shape>& shapes, double& xmin, double& ymin, double& zmin,
             double& xmax, double& ymax, double& zmax) {
    Bnd_Box box;
    for (auto& s : shapes) BRepBndLib::Add(s, box);
    box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
}

// discretize an edge to a polyline
std::vector<std::array<double, 3>> edge_points(const TopoDS_Edge& e, double defl) {
    std::vector<std::array<double, 3>> pts;
    BRepAdaptor_Curve c(e);
    GCPnts_UniformDeflection d(c, defl);
    if (d.IsDone())
        for (int i = 1; i <= d.NbPoints(); ++i) {
            gp_Pnt p = d.Value(i);
            pts.push_back({p.X(), p.Y(), p.Z()});
        }
    return pts;
}

// chain section edges (at constant Z) into closed XY loops
json section_regions(const std::vector<TopoDS_Shape>& shapes, double z, double defl) {
    json loops = json::array();
    gp_Pln plane(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1));
    std::vector<std::vector<std::array<double, 2>>> segs;
    for (auto& s : shapes) {
        BRepAlgoAPI_Section sec(s, plane, Standard_False);
        sec.ComputePCurveOn1(Standard_False);
        sec.Approximation(Standard_True);
        sec.Build();
        if (!sec.IsDone()) continue;
        TopExp_Explorer ex(sec.Shape(), TopAbs_EDGE);
        for (; ex.More(); ex.Next()) {
            auto pts = edge_points(TopoDS::Edge(ex.Current()), defl);
            if (pts.size() < 2) continue;
            std::vector<std::array<double, 2>> poly;
            for (auto& p : pts) poly.push_back({p[0], p[1]});
            segs.push_back(std::move(poly));
        }
    }
    // greedy chain segments end-to-end into closed loops
    const double tol = 1e-3;
    auto dist = [](const std::array<double, 2>& a, const std::array<double, 2>& b) {
        return std::hypot(a[0] - b[0], a[1] - b[1]);
    };
    std::vector<bool> used(segs.size(), false);
    for (size_t i = 0; i < segs.size(); ++i) {
        if (used[i] || segs[i].size() < 2) continue;
        used[i] = true;
        std::vector<std::array<double, 2>> chain = segs[i];
        bool extended = true;
        while (extended) {
            extended = false;
            for (size_t j = 0; j < segs.size(); ++j) {
                if (used[j]) continue;
                if (dist(chain.back(), segs[j].front()) < tol) {
                    for (size_t k = 1; k < segs[j].size(); ++k) chain.push_back(segs[j][k]);
                    used[j] = true; extended = true;
                } else if (dist(chain.back(), segs[j].back()) < tol) {
                    for (size_t k = segs[j].size(); k-- > 1;) chain.push_back(segs[j][k - 1]);
                    used[j] = true; extended = true;
                }
            }
        }
        if (chain.size() < 3) continue;
        json outer = json::array();
        for (auto& p : chain) outer.push_back(json::array({p[0], p[1]}));
        loops.push_back(json{{"outer", outer}, {"holes", json::array()}});
    }
    // nest holes: a loop fully inside another is that one's hole (simple area sort)
    auto area = [](const json& loop) {
        const json& o = loop["outer"];
        double a = 0;
        size_t n = o.size();
        for (size_t i = 0; i < n; ++i) {
            double x1 = o[i][0], y1 = o[i][1];
            double x2 = o[(i + 1) % n][0], y2 = o[(i + 1) % n][1];
            a += x1 * y2 - x2 * y1;
        }
        return std::fabs(a) * 0.5;
    };
    // sort by area descending; attach smaller loops contained in a larger one as holes
    std::vector<size_t> order(loops.size());
    for (size_t i = 0; i < order.size(); ++i) order[i] = i;
    std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return area(loops[a]) > area(loops[b]); });
    auto pip = [](double px, double py, const json& o) {
        bool in = false;
        size_t n = o.size();
        for (size_t i = 0, j = n - 1; i < n; j = i++) {
            double xi = o[i][0], yi = o[i][1], xj = o[j][0], yj = o[j][1];
            if (((yi > py) != (yj > py)) && (px < (xj - xi) * (py - yi) / (yj - yi) + xi)) in = !in;
        }
        return in;
    };
    json result = json::array();
    std::vector<bool> consumed(loops.size(), false);
    for (size_t oi = 0; oi < order.size(); ++oi) {
        size_t a = order[oi];
        if (consumed[a]) continue;
        json reg = loops[a];
        for (size_t oj = oi + 1; oj < order.size(); ++oj) {
            size_t b = order[oj];
            if (consumed[b]) continue;
            double px = loops[b]["outer"][0][0], py = loops[b]["outer"][0][1];
            if (pip(px, py, reg["outer"])) { reg["holes"].push_back(loops[b]["outer"]); consumed[b] = true; }
        }
        consumed[a] = true;
        result.push_back(reg);
    }
    return result;
}

json mesh_json(const std::vector<TopoDS_Shape>& shapes, double defl) {
    json verts = json::array(), tris = json::array();
    int base = 0;
    for (auto& s : shapes) {
        doc::TessOptions to;
        to.lin_deflection = defl;
        doc::MeshData md = doc::shape_to_mesh(s, to);
        for (double v : md.verts) verts.push_back(v);
        for (uint32_t i : md.tris) tris.push_back((int)i + base);
        base += (int)md.vertex_count();
    }
    return json{{"verts", verts}, {"tris", tris}};
}

// Hole extraction for auto-drill: recognizes plain holes, counterbores
// (coaxial cylinders of different diameter), countersinks (cone faces), and tilted holes.
// Tilt allowance is 0.95 (was 0.99 = Z-only); the tilt angle is emitted so the caller can
// decide whether a 3-axis machine can program it.
json holes_json(const std::vector<TopoDS_Shape>& shapes) {
    constexpr double PI = 3.14159265358979323846;
    constexpr double AXIS_TOL = 0.95;  // |axis.Z()| >= this counts as a drillable (near-Z) hole
    constexpr double XY_TOL = 1e-2;     // coaxial-grouping tolerance for axis XY

    struct Cyl { double x, y, ztop, zbot, r, tilt; };
    struct Cone { double x, y, angle_deg, outer_dia; };
    std::vector<Cyl> cyls;
    std::vector<Cone> cones;

    for (auto& s : shapes) {
        for (TopExp_Explorer ex(s, TopAbs_FACE); ex.More(); ex.Next()) {
            TopoDS_Face f = TopoDS::Face(ex.Current());
            BRepAdaptor_Surface surf(f);
            GeomAbs_SurfaceType st = surf.GetType();
            if (st != GeomAbs_Cylinder && st != GeomAbs_Cone) continue;
            Bnd_Box fb;
            BRepBndLib::Add(f, fb);
            double x0, y0, z0, x1, y1, z1;
            fb.Get(x0, y0, z0, x1, y1, z1);

            if (st == GeomAbs_Cylinder) {
                gp_Cylinder cyl = surf.Cylinder();
                gp_Dir ax = cyl.Axis().Direction();
                double az = std::fabs(ax.Z());
                if (az < AXIS_TOL) continue;
                gp_Pnt loc = cyl.Axis().Location();
                double tilt = std::acos(std::min(1.0, az)) * 180.0 / PI;
                Cyl h{loc.X(), loc.Y(), z1, z0, cyl.Radius(), tilt};
                // merge coaxial faces of the SAME radius (a cylinder split into seam faces)
                bool dup = false;
                for (auto& e : cyls)
                    if (std::hypot(e.x - h.x, e.y - h.y) < XY_TOL && std::fabs(e.r - h.r) < 1e-3) {
                        e.ztop = std::max(e.ztop, h.ztop); e.zbot = std::min(e.zbot, h.zbot); dup = true; break;
                    }
                if (!dup) cyls.push_back(h);
            } else {  // GeomAbs_Cone -> countersink candidate
                gp_Cone cn = surf.Cone();
                gp_Dir ax = cn.Axis().Direction();
                if (std::fabs(ax.Z()) < AXIS_TOL) continue;  // only ~Z-axis countersinks are drillable
                gp_Pnt loc = cn.Axis().Location();
                double angle_deg = 2.0 * std::fabs(cn.SemiAngle()) * 180.0 / PI;  // included (tip) angle
                double outer_dia = std::max(x1 - x0, y1 - y0);  // widest ring of the cone face
                cones.push_back(Cone{loc.X(), loc.Y(), angle_deg, outer_dia});
            }
        }
    }

    // Group coaxial cylinders (same axis XY) — a group with >1 distinct radius is a counterbore.
    std::vector<std::vector<int>> groups;
    std::vector<bool> used(cyls.size(), false);
    for (size_t i = 0; i < cyls.size(); ++i) {
        if (used[i]) continue;
        std::vector<int> g{static_cast<int>(i)};
        used[i] = true;
        for (size_t j = i + 1; j < cyls.size(); ++j)
            if (!used[j] && std::hypot(cyls[i].x - cyls[j].x, cyls[i].y - cyls[j].y) < XY_TOL) {
                g.push_back(static_cast<int>(j));
                used[j] = true;
            }
        groups.push_back(std::move(g));
    }

    json points = json::array(), holes = json::array();
    for (auto& g : groups) {
        std::sort(g.begin(), g.end(), [&](int a, int b) { return cyls[a].r < cyls[b].r; });
        const Cyl& minor = cyls[g.front()];  // smallest dia = the drilled through-hole
        const Cyl& major = cyls[g.back()];   // largest dia
        double ztop = -1e30, zbot = 1e30;
        for (int idx : g) { ztop = std::max(ztop, cyls[idx].ztop); zbot = std::min(zbot, cyls[idx].zbot); }
        // Report AXIAL depth (bore length along the tool axis), not the Z-projection: for a
        // tilted hole the drilled depth a driller needs is z_extent / cos(tilt). |axis.Z()| =
        // cos(tilt); guarded so a (near-)horizontal axis can't divide by ~0. Vertical holes
        // (tilt≈0, cos≈1) are unchanged, so counterbore depth_major/minor stay as before.
        const double axz = std::max(1e-3, std::cos(minor.tilt * PI / 180.0));

        json rec{{"x", minor.x}, {"y", minor.y}, {"z", ztop}, {"dia", minor.r * 2},
                 {"depth", (ztop - zbot) / axz}, {"tilt_deg", minor.tilt}};
        std::string kind = "simple";
        if (g.size() >= 2 && std::fabs(major.r - minor.r) > 1e-3) {
            kind = "counterbore";
            rec["dia_major"] = major.r * 2;
            rec["dia_minor"] = minor.r * 2;
            rec["depth_major"] = (major.ztop - major.zbot) / axz;
            rec["depth_minor"] = (minor.ztop - minor.zbot) / axz;
        }
        // Attach a coaxial cone -> countersink on this hole.
        for (auto& cn : cones)
            if (std::hypot(cn.x - minor.x, cn.y - minor.y) < XY_TOL) {
                rec["csink_angle_deg"] = cn.angle_deg;
                rec["csink_outer_dia"] = cn.outer_dia;
                kind = (kind == "counterbore") ? "counterbore_countersink" : "countersink";
                break;
            }
        rec["kind"] = kind;
        points.push_back(json::array({minor.x, minor.y, ztop}));
        holes.push_back(rec);
    }

    // Standalone countersinks (a cone with no coaxial cylinder) — rare, but record them.
    for (auto& cn : cones) {
        bool matched = false;
        for (auto& g : groups)
            if (std::hypot(cn.x - cyls[g.front()].x, cn.y - cyls[g.front()].y) < XY_TOL) { matched = true; break; }
        if (matched) continue;
        holes.push_back(json{{"x", cn.x}, {"y", cn.y}, {"kind", "countersink"},
                             {"csink_angle_deg", cn.angle_deg}, {"csink_outer_dia", cn.outer_dia}});
    }

    return json{{"points", points}, {"holes", holes}};
}

// +X turning silhouette from a Y=0 section (radius vs axial Z)
json profile_json(const std::vector<TopoDS_Shape>& shapes, double defl) {
    gp_Pln plane(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0));  // plane normal +Y -> the XZ plane
    std::vector<std::array<double, 2>> pts;  // (z, x)
    for (auto& s : shapes) {
        BRepAlgoAPI_Section sec(s, plane, Standard_False);
        sec.Approximation(Standard_True);
        sec.Build();
        if (!sec.IsDone()) continue;
        TopExp_Explorer ex(sec.Shape(), TopAbs_EDGE);
        for (; ex.More(); ex.Next()) {
            auto ep = edge_points(TopoDS::Edge(ex.Current()), defl);
            for (auto& p : ep) if (p[0] >= -1e-6) pts.push_back({p[2], std::fabs(p[0])});  // x>=0 half
        }
    }
    std::sort(pts.begin(), pts.end(), [](auto& a, auto& b) { return a[0] < b[0]; });
    json prof = json::array();
    for (auto& p : pts) prof.push_back(json::array({p[0], p[1]}));
    return prof;
}

}  // namespace

json cam_extract(const json& params) {
    Loaded L = load(params);
    if (!L.ok) return json{{"error", L.error}};
    std::string kind = params.value("kind", std::string("all"));
    double defl = params.value("deflection", 0.1);
    double xmin, ymin, zmin, xmax, ymax, zmax;
    bbox_of(L.shapes, xmin, ymin, zmin, xmax, ymax, zmax);
    json bbox{{"min", json::array({xmin, ymin, zmin})}, {"max", json::array({xmax, ymax, zmax})}};

    json out;
    out["bbox"] = bbox;
    auto add_stock = [&]() {
        double cx = (xmin + xmax) * 0.5, cy = (ymin + ymax) * 0.5;
        double r = 0.5 * std::max(xmax - xmin, ymax - ymin);
        out["stock"] = json{
            {"box", json{{"min", json::array({xmin, ymin, zmin})}, {"max", json::array({xmax, ymax, zmax})}}},
            {"cylinder", json{{"base", json::array({cx, cy, zmin})}, {"radius", r}, {"height", zmax - zmin}}}};
    };

    try {
        if (kind == "stock") { add_stock(); }
        else if (kind == "mesh") { out["mesh"] = mesh_json(L.shapes, defl); }
        else if (kind == "section") {
            double z = params.value("z", zmax);
            out["regions"] = section_regions(L.shapes, z, defl);
        } else if (kind == "sections") {
            json levels = json::array();
            if (params.contains("z_levels"))
                for (auto& zj : params["z_levels"]) {
                    double z = zj.get<double>();
                    levels.push_back(json{{"z", z}, {"regions", section_regions(L.shapes, z, defl)}});
                }
            out["levels"] = levels;
        } else if (kind == "holes") {
            json h = holes_json(L.shapes);
            out["points"] = h["points"]; out["holes"] = h["holes"];
        } else if (kind == "profile") {
            out["profile"] = profile_json(L.shapes, defl);
        } else {  // "all"
            add_stock();
            out["mesh"] = mesh_json(L.shapes, defl);
            json h = holes_json(L.shapes);
            out["points"] = h["points"]; out["holes"] = h["holes"];
        }
    } catch (const std::exception& e) {
        return json{{"error", std::string("cam_extract failed: ") + e.what()}};
    }
    return out;
}

}  // namespace cam
}  // namespace chudware
