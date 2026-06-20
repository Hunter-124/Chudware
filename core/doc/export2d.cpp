// 2D drawing + export RPCs — see export2d.h.

#include "doc/export2d.h"

#include <array>
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepGProp.hxx>
#include <BRepTools.hxx>
#include <BRepTools_WireExplorer.hxx>
#include <BRep_Tool.hxx>
#include <GCPnts_TangentialDeflection.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Pnt.hxx>
#include <algorithm>
#include <cmath>

#include "geom/booleans.h"  // unify_same_domain (merge the flat blank's coplanar faces)

#include "doc/document.h"
#include "doc/drawing.h"
#include "doc/drawing_session.h"  // drawing_get (stateful export_drawing)
#include "doc/dxf.h"
#include "doc/pdfvec.h"
#include "doc/session.h"  // OpError, doc_live_source

using nlohmann::json;

namespace aicad::doc {

namespace {

std::array<double, 2> get_pos(const json& v) {
    if (v.contains("position") && v["position"].is_array() && v["position"].size() >= 2)
        return {v["position"][0].get<double>(), v["position"][1].get<double>()};
    return {0.0, 0.0};
}

// Pool one polyline array (already absolute sheet-mm) into `out`. Skips degenerate
// (< 2 point) polylines. Shared by export2d_drawing + the stateful export_drawing.
void pool_polylines(const json& jpls, std::vector<Polyline>& out) {
    if (!jpls.is_array()) return;
    for (const auto& jpl : jpls) {
        if (!jpl.is_array() || jpl.size() < 2) continue;
        Polyline pl;
        for (const auto& jp : jpl) pl.push_back({jp[0].get<double>(), jp[1].get<double>()});
        out.push_back(std::move(pl));
    }
}

// Write {VISIBLE, HIDDEN} polyline pools to a DXF/PDF file. Shared writer for the
// stateless + stateful drawing exports. Returns the written path.
std::string write_drawing_layers(const std::vector<Polyline>& visible,
                                 const std::vector<Polyline>& hidden, const std::string& path,
                                 const std::string& fmt) {
    if (fmt == "dxf") {
        DxfLayers layers;
        layers.add_layer("VISIBLE");  // insertion order VISIBLE then HIDDEN (Python dict order)
        layers.add_layer("HIDDEN");
        for (const auto& pl : visible) layers.add_polyline("VISIBLE", pl);
        for (const auto& pl : hidden) layers.add_polyline("HIDDEN", pl);
        return write_dxf(path, layers);
    }
    std::vector<PdfLine> lines;
    for (const auto& pl : visible) lines.push_back({pl, false});
    for (const auto& pl : hidden) lines.push_back({pl, true});
    return write_pdf(path, lines);
}

// Project every view def against `shapes`, returning the list of project_view JSON.
json project_all(const std::vector<TopoDS_Shape>& shapes, const json& views) {
    json out = json::array();
    int idx = 0;
    for (const auto& v : views) {
        std::string orient = v.value("orientation", std::string("front"));
        double scale = v.value("scale", 1.0);
        bool show_hidden = v.value("show_hidden", true);
        std::string vid = v.value("view_id", std::string("v") + std::to_string(++idx));
        out.push_back(project_view(shapes, orient, scale, get_pos(v), show_hidden, vid));
    }
    return out;
}

}  // namespace

namespace {

using Vec3 = std::array<double, 3>;

// Orthonormal (u, v, n) frame for projecting base-plane 3D points to 2D — mirrors
// export2d.py::_basis_from_normal (u = seed×n with seed the world axis least ∥ n).
void basis_from_normal(const Vec3& n_in, Vec3& u, Vec3& v, Vec3& n) {
    double nx = n_in[0], ny = n_in[1], nz = n_in[2];
    double ln = std::sqrt(nx * nx + ny * ny + nz * nz);
    if (ln < 1e-30) ln = 1.0;
    nx /= ln; ny /= ln; nz /= ln;
    Vec3 seed = (std::fabs(nx) < 0.9) ? Vec3{1, 0, 0} : Vec3{0, 1, 0};
    double ux = seed[1] * nz - seed[2] * ny;
    double uy = seed[2] * nx - seed[0] * nz;
    double uz = seed[0] * ny - seed[1] * nx;
    double lu = std::sqrt(ux * ux + uy * uy + uz * uz);
    if (lu < 1e-30) lu = 1.0;
    ux /= lu; uy /= lu; uz /= lu;
    u = {ux, uy, uz};
    v = {ny * uz - nz * uy, nz * ux - nx * uz, nx * uy - ny * ux};
    n = {nx, ny, nz};
}

std::array<double, 2> to_2d(const gp_Pnt& p, const Vec3& u, const Vec3& v) {
    return {p.X() * u[0] + p.Y() * u[1] + p.Z() * u[2],
            p.X() * v[0] + p.Y() * v[1] + p.Z() * v[2]};
}

// Walk a wire's edges in connected order, discretizing each into 2D points (lines -> 2
// points; curves -> tangential-deflection samples), and close the loop. Shared vertices
// between consecutive edges are de-duplicated.
Polyline wire_to_polyline(const TopoDS_Wire& wire, const Vec3& u, const Vec3& v) {
    Polyline pl;
    auto push = [&](std::array<double, 2> p) {
        if (pl.empty() ||
            std::fabs(pl.back()[0] - p[0]) > 1e-7 || std::fabs(pl.back()[1] - p[1]) > 1e-7)
            pl.push_back(p);
    };
    for (BRepTools_WireExplorer wx(wire); wx.More(); wx.Next()) {
        const TopoDS_Edge& e = wx.Current();
        BRepAdaptor_Curve ac(e);
        GCPnts_TangentialDeflection disc(ac, 0.2 /*ang*/, 0.1 /*lin*/);
        int npts = disc.NbPoints();
        if (npts < 2) continue;
        std::vector<std::array<double, 2>> samples;
        samples.reserve(npts);
        for (int i = 1; i <= npts; ++i) samples.push_back(to_2d(disc.Value(i), u, v));
        if (e.Orientation() == TopAbs_REVERSED)
            std::reverse(samples.begin(), samples.end());
        for (const auto& s : samples) push(s);
    }
    // Close the loop (DXF/PDF read the blank boundary as closed).
    if (pl.size() >= 2 &&
        (std::fabs(pl.front()[0] - pl.back()[0]) > 1e-7 ||
         std::fabs(pl.front()[1] - pl.back()[1]) > 1e-7))
        pl.push_back(pl.front());
    return pl;
}

// The broad (largest-area) planar face of the flat solid whose normal is ∥ n_base.
// Its outer wire is the blank OUTLINE; its inner wires are punched CUTS.
TopoDS_Face broad_face(const TopoDS_Shape& solid, const Vec3& n_base, double& out_area) {
    TopoDS_Face best;
    double best_area = -1.0;
    for (TopExp_Explorer ex(solid, TopAbs_FACE); ex.More(); ex.Next()) {
        TopoDS_Face f = TopoDS::Face(ex.Current());
        BRepAdaptor_Surface surf(f);
        if (surf.GetType() != GeomAbs_Plane) continue;
        gp_Dir nd = surf.Plane().Axis().Direction();
        double dot = std::fabs(nd.X() * n_base[0] + nd.Y() * n_base[1] + nd.Z() * n_base[2]);
        if (dot < 0.99) continue;  // not parallel to the base plane
        GProp_GProps g;
        BRepGProp::SurfaceProperties(f, g);
        double a = g.Mass();
        if (a > best_area) { best_area = a; best = f; }
    }
    out_area = best_area > 0 ? best_area : 0.0;
    return best;
}

}  // namespace

json drawing_views(const json& doc, const json& env, const json& views) {
    if (!views.is_array())
        throw OpError("invalid_op", "drawing_views: 'views' must be an array");
    CompileResult res = compile_document(doc, env.is_null() ? json::object() : env);
    json out = {{"ok", res.ok}, {"body_count", res.body_count}};
    out["views"] = project_all(res.shapes, views);
    return out;
}

json export2d_drawing(const json& doc, const json& env, const json& views,
                      const std::string& path, const std::string& fmt) {
    if (!views.is_array())
        throw OpError("invalid_op", "export2d_drawing: 'views' must be an array");
    if (fmt != "dxf" && fmt != "pdf")
        throw OpError("invalid_op", "format must be 'dxf' or 'pdf'", {{"format", fmt}});
    CompileResult res = compile_document(doc, env.is_null() ? json::object() : env);
    json projected = project_all(res.shapes, views);

    // Pool visible + hidden polylines (already absolute sheet mm). Mirrors
    // export2d.py::_drawing_layers.
    std::vector<Polyline> visible, hidden;
    for (const auto& view : projected) {
        pool_polylines(view.value("visible", json::array()), visible);
        pool_polylines(view.value("hidden", json::array()), hidden);
    }
    int n = static_cast<int>(visible.size() + hidden.size());
    if (n == 0)
        throw OpError("export_failed",
                      "export2d_drawing: the drawing has no view geometry to export",
                      {{"path", path}});

    std::string out_path;
    try {
        out_path = write_drawing_layers(visible, hidden, path, fmt);
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& exc) {
        throw OpError("export_failed", std::string("export2d_drawing: ") + exc.what(),
                      {{"path", path}, {"format", fmt}});
    }

    return {{"path", out_path}, {"format", fmt},
            {"visible", static_cast<int>(visible.size())},
            {"hidden", static_cast<int>(hidden.size())}, {"entities", n}};
}

json export_drawing(const std::string& doc_id, const std::string& path, const std::string& fmt) {
    if (fmt != "dxf" && fmt != "pdf")
        throw OpError("invalid_op", "format must be 'dxf' or 'pdf'", {{"format", fmt}});
    // Re-project the open drawing (validates the dwg-cpp doc; raises unknown_doc otherwise).
    json full = drawing_get(doc_id);

    // Pool every sheet/view's visible+hidden polylines (already absolute sheet mm) —
    // mirrors export2d.py::_drawing_layers / export_drawing.
    std::vector<Polyline> visible, hidden;
    if (full.contains("sheets") && full["sheets"].is_array())
        for (const auto& sheet : full["sheets"])
            if (sheet.contains("views") && sheet["views"].is_array())
                for (const auto& view : sheet["views"]) {
                    pool_polylines(view.value("visible", json::array()), visible);
                    pool_polylines(view.value("hidden", json::array()), hidden);
                }
    int n = static_cast<int>(visible.size() + hidden.size());
    if (n == 0)
        throw OpError("export_failed",
                      "export_drawing: the drawing has no view geometry to export "
                      "(add views first, then Rebuild)",
                      {{"doc_id", doc_id}});

    std::string out_path;
    try {
        out_path = write_drawing_layers(visible, hidden, path, fmt);
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& exc) {
        throw OpError("export_failed", std::string("export_drawing: ") + exc.what(),
                      {{"doc_id", doc_id}, {"path", path}, {"format", fmt}});
    }
    return {{"path", out_path}, {"format", fmt},
            {"visible", static_cast<int>(visible.size())},
            {"hidden", static_cast<int>(hidden.size())}, {"entities", n}};
}

json export_flat_dxf(const std::string& doc_id, const std::string& path, const std::string& fmt) {
    if (fmt != "dxf" && fmt != "pdf")
        throw OpError("invalid_op", "format must be 'dxf' or 'pdf'", {{"format", fmt}});
    // Fresh compile of the live source part (mirrors flat_pattern_for_session's fresh
    // cache so sm_flat_pattern repopulates the flat side-channel).
    json live = doc_live_source(doc_id);
    if (!live.is_object() || !live.contains("tree"))
        throw OpError("unknown_doc", "export_flat_dxf: no open document '" + doc_id + "'",
                      {{"doc_id", doc_id}});
    CompileResult res = compile_document(live["tree"], live.value("env", json::object()));
    if (res.sheet_metal_flat.empty() || res.shapes.empty())
        throw OpError("invalid_op",
                      "export_flat_dxf: the part has no flat pattern — add a Flatten "
                      "(sm_flat_pattern) feature to a sheet-metal part first",
                      {{"reason", "no flat pattern"}, {"doc_id", doc_id}});
    // Use the LAST flat record (most recent Flatten) — json preserves insertion order.
    json flat_rec;
    for (auto it = res.sheet_metal_flat.begin(); it != res.sheet_metal_flat.end(); ++it)
        flat_rec = it.value();
    Vec3 n_base{0, 0, 1};
    if (flat_rec.contains("n_base") && flat_rec["n_base"].is_array() &&
        flat_rec["n_base"].size() == 3)
        n_base = {flat_rec["n_base"][0].get<double>(), flat_rec["n_base"][1].get<double>(),
                  flat_rec["n_base"][2].get<double>()};
    Vec3 u, v, n;
    basis_from_normal(n_base, u, v, n);

    // The flat blank is the surviving body (flatten is terminal). Merge its coplanar
    // faces (the fused base block + developed strips arrive as SEPARATE coplanar faces)
    // so the broad face is the WHOLE developed blank — its outer wire is the true OUTLINE
    // and its inner wires the CUTS. (build123d's .clean() does the same in the oracle.)
    const TopoDS_Shape flat_solid = ::geom::unify_same_domain(res.shapes.back());
    double flat_area = 0.0;
    TopoDS_Face bf = broad_face(flat_solid, n, flat_area);
    if (bf.IsNull())
        throw OpError("export_failed",
                      "export_flat_dxf: the flat pattern produced no broad face to project",
                      {{"doc_id", doc_id}});

    TopoDS_Wire outer = BRepTools::OuterWire(bf);
    Polyline outline = wire_to_polyline(outer, u, v);
    std::vector<Polyline> cuts;
    for (TopExp_Explorer wx(bf, TopAbs_WIRE); wx.More(); wx.Next()) {
        TopoDS_Wire w = TopoDS::Wire(wx.Current());
        if (w.IsSame(outer)) continue;
        Polyline c = wire_to_polyline(w, u, v);
        if (c.size() >= 2) cuts.push_back(std::move(c));
    }
    if (outline.size() < 2)
        throw OpError("export_failed",
                      "export_flat_dxf: the flat pattern produced no outline geometry",
                      {{"doc_id", doc_id}});

    // Flat-space bend seam lines -> 2D segments (BEND layer).
    std::vector<Polyline> bend_segs;
    if (flat_rec.contains("bend_lines") && flat_rec["bend_lines"].is_array())
        for (const auto& seg : flat_rec["bend_lines"]) {
            if (!seg.is_array() || seg.size() != 2) continue;
            auto p2 = [&](const json& p) {
                gp_Pnt gp(p[0].get<double>(), p[1].get<double>(), p[2].get<double>());
                return to_2d(gp, u, v);
            };
            bend_segs.push_back({p2(seg[0]), p2(seg[1])});
        }

    int n_ent = 1 + static_cast<int>(bend_segs.size()) + static_cast<int>(cuts.size());
    std::string out_path;
    try {
        if (fmt == "dxf") {
            DxfLayers layers;
            layers.add_layer("OUTLINE");  // insertion order matches export2d.py dict order
            layers.add_layer("BEND");
            layers.add_layer("CUTS");
            layers.add_polyline("OUTLINE", outline);
            for (const auto& s : bend_segs) layers.add_polyline("BEND", s);
            for (const auto& c : cuts) layers.add_polyline("CUTS", c);
            out_path = write_dxf(path, layers);
        } else {
            // Outline solid; bend lines dashed (PDF has no named layers — keep them visually
            // distinct). Cut loops solid (they are real cut boundaries).
            std::vector<PdfLine> lines;
            lines.push_back({outline, false});
            for (const auto& c : cuts) lines.push_back({c, false});
            for (const auto& s : bend_segs) lines.push_back({s, true});
            out_path = write_pdf(path, lines);
        }
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& exc) {
        throw OpError("export_failed", std::string("export_flat_dxf: ") + exc.what(),
                      {{"doc_id", doc_id}, {"path", path}, {"format", fmt}});
    }
    return {{"path", out_path}, {"format", fmt},
            {"bend_lines", static_cast<int>(bend_segs.size())},
            {"outline_points", static_cast<int>(outline.size())},
            {"flat_area", flat_area}, {"entities", n_ent}};
}

}  // namespace aicad::doc
