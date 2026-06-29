// core/cam/extract.cpp — OCCT implementation of the CAM geometry extraction
// adapter (declared OCCT-free in cam/extract.h). Lives in the aicad_doc lib, which
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
#include <gp_Cylinder.hxx>
#include <gp_Pln.hxx>

#include "doc/formats.h"

namespace aicad {
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

json holes_json(const std::vector<TopoDS_Shape>& shapes) {
    json points = json::array(), holes = json::array();
    struct H { double x, y, ztop, zbot, r; };
    std::vector<H> found;
    for (auto& s : shapes) {
        TopExp_Explorer ex(s, TopAbs_FACE);
        for (; ex.More(); ex.Next()) {
            TopoDS_Face f = TopoDS::Face(ex.Current());
            BRepAdaptor_Surface surf(f);
            if (surf.GetType() != GeomAbs_Cylinder) continue;
            gp_Cylinder cyl = surf.Cylinder();
            gp_Dir ax = cyl.Axis().Direction();
            if (std::fabs(ax.Z()) < 0.99) continue;  // only Z-axis holes for 3-axis drilling
            gp_Pnt loc = cyl.Axis().Location();
            Bnd_Box fb;
            BRepBndLib::Add(f, fb);
            double x0, y0, z0, x1, y1, z1;
            fb.Get(x0, y0, z0, x1, y1, z1);
            H h{loc.X(), loc.Y(), z1, z0, cyl.Radius()};
            // dedup by axis xy + radius
            bool dup = false;
            for (auto& e : found)
                if (std::hypot(e.x - h.x, e.y - h.y) < 1e-3 && std::fabs(e.r - h.r) < 1e-3) {
                    e.ztop = std::max(e.ztop, h.ztop); e.zbot = std::min(e.zbot, h.zbot); dup = true; break;
                }
            if (!dup) found.push_back(h);
        }
    }
    for (auto& h : found) {
        points.push_back(json::array({h.x, h.y, h.ztop}));
        holes.push_back(json{{"x", h.x}, {"y", h.y}, {"z", h.ztop}, {"dia", h.r * 2},
                             {"depth", h.ztop - h.zbot}});
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
}  // namespace aicad
