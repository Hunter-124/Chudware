// 2D drawing + export RPCs — see export2d.h.

#include "doc/export2d.h"
#include <array>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <utility>
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
#include "doc/dwg.h"
#include "doc/dxf.h"
#include "doc/pdfvec.h"
#include "doc/svgvec.h"
#include "doc/session.h"  // OpError, doc_live_source

using nlohmann::json;

namespace chudware::doc {

namespace {

std::array<double, 2> get_pos(const json& v) {
    if (v.contains("position") && v["position"].is_array() && v["position"].size() >= 2)
        return {v["position"][0].get<double>(), v["position"][1].get<double>()};
    return {0.0, 0.0};
}

bool read_pt(const json& p, InkPoint& out) {
    if (!p.is_array() || p.size() < 2 || !p[0].is_number() || !p[1].is_number()) return false;
    out = {p[0].get<double>(), p[1].get<double>()};
    return true;
}

// Pool a JSON array of polylines ([[x,y],...] per entry) into `dst`; < 2 points skipped.
void pool_polylines(const json& arr, std::vector<InkPolyline>& dst) {
    if (!arr.is_array()) return;
    for (const auto& jpl : arr) {
        if (!jpl.is_array() || jpl.size() < 2) continue;
        InkPolyline pl;
        pl.reserve(jpl.size());
        InkPoint q;
        for (const auto& jp : jpl)
            if (read_pt(jp, q)) pl.push_back(q);
        if (pl.size() >= 2) dst.push_back(std::move(pl));
    }
}

void pool_key(const json& block, const char* key, std::vector<InkPolyline>& dst) {
    auto it = block.find(key);
    if (it != block.end()) pool_polylines(*it, dst);
}

// Pool one v2 draw block (segments/thick/dashed/chain/chain_thick/arcs/tris/texts).
void pool_draw(const json& d, InkLayer& L) {
    if (!d.is_object()) return;
    pool_key(d, "segments", L.thin);
    pool_key(d, "thick", L.thick);
    pool_key(d, "dashed", L.dashed);
    pool_key(d, "chain", L.chain);
    pool_key(d, "chain_thick", L.chain_thick);
    if (auto it = d.find("arcs"); it != d.end() && it->is_array())
        for (const auto& a : *it) {
            if (!a.is_object()) continue;
            InkArc arc;
            auto c = a.find("center");
            if (c == a.end() || !read_pt(*c, arc.center)) continue;
            arc.radius = a.value("radius", 0.0);
            arc.a0 = a.value("a0", 0.0);
            arc.a1 = a.value("a1", 2.0 * 3.14159265358979323846);
            if (arc.radius > 0.0) L.arcs.push_back(arc);
        }
    if (auto it = d.find("tris"); it != d.end() && it->is_array())
        for (const auto& t : *it) {
            if (!t.is_array() || t.size() != 3) continue;
            std::array<InkPoint, 3> tri;
            if (read_pt(t[0], tri[0]) && read_pt(t[1], tri[1]) && read_pt(t[2], tri[2]))
                L.tris.push_back(tri);
        }
    if (auto it = d.find("texts"); it != d.end() && it->is_array())
        for (const auto& t : *it) {
            if (!t.is_object()) continue;
            InkText txt;
            auto p = t.find("pos");
            if (p == t.end() || !read_pt(*p, txt.pos)) continue;
            auto s = t.find("text");
            if (s == t.end() || !s->is_string()) continue;
            txt.text = s->get<std::string>();
            txt.h = t.value("h", 3.5);
            if (txt.text.empty() || !(txt.h > 0.0)) continue;
            const std::string align = t.value("align", std::string("left"));
            const std::string valign = t.value("valign", std::string("base"));
            txt.align = align == "center" ? InkAlign::center
                      : align == "right"  ? InkAlign::right
                                          : InkAlign::left;
            txt.valign = valign == "mid" ? InkVAlign::mid
                       : valign == "top" ? InkVAlign::top
                                         : InkVAlign::base;
            L.texts.push_back(std::move(txt));
        }
}

// --- DXF -------------------------------------------------------------------------

// Every sheet drawing declares the full layer set (empty layers are harmless).
void declare_drawing_layers(DxfDocument& doc) {
    doc.add_layer("VISIBLE", 7, "CONTINUOUS");
    doc.add_layer("HIDDEN", 8, "DASHED");
    doc.add_layer("CENTER", 1, "CENTER");
    doc.add_layer("DIMENSIONS", 3, "CONTINUOUS");
    doc.add_layer("HATCH", 8, "CONTINUOUS");
    doc.add_layer("BORDER", 7, "CONTINUOUS");
    doc.add_layer("TABLES", 7, "CONTINUOUS");
}

// Emit one ink layer onto DXF layer `name` (linetype `layer_ltype`). Chain lines go to
// CENTER when `chain_layer` is set, otherwise stay on `name` with a CENTER override.
void ink_layer_to_dxf(const InkLayer& l, const std::string& name, const char* layer_ltype,
                      const char* chain_layer, DxfDocument& doc) {
    auto ov = [&](const char* lt) { return std::string(lt) == layer_ltype ? "" : lt; };
    auto add = [&](const std::vector<InkPolyline>& v, const std::string& layer, double width,
                   const std::string& lt) {
        for (const auto& pl : v) doc.polylines.push_back({layer, pl, width, lt});
    };
    add(l.thin, name, 0.0, ov("CONTINUOUS"));
    add(l.thick, name, kInkThick, ov("CONTINUOUS"));
    add(l.dashed, name, 0.0, ov("DASHED"));
    const std::string chain_name = chain_layer ? chain_layer : name;
    const std::string chain_lt = chain_layer ? "" : ov("CENTER");
    add(l.chain, chain_name, 0.0, chain_lt);
    add(l.chain_thick, chain_name, kInkThick, chain_lt);
    constexpr double kDeg = 180.0 / 3.14159265358979323846;
    for (const InkArc& a : l.arcs) {
        DxfArc arc;
        arc.layer = name;
        arc.center = a.center;
        arc.radius = a.radius;
        arc.circle = a.full();
        arc.start_deg = a.a0 * kDeg;
        arc.end_deg = (a.a0 + a.sweep()) * kDeg;
        arc.ltype = ov("CONTINUOUS");
        doc.arcs.push_back(std::move(arc));
    }
    for (const auto& t : l.tris) doc.solids.push_back({name, t});
    for (const InkText& t : l.texts) {
        DxfText dt;
        dt.layer = name;
        dt.pos = t.pos;
        dt.height = t.h;
        dt.text = t.text;
        dt.halign = t.align == InkAlign::center ? 1 : t.align == InkAlign::right ? 2 : 0;
        dt.valign = t.valign == InkVAlign::mid ? 2 : t.valign == InkVAlign::top ? 3 : 0;
        doc.texts.push_back(std::move(dt));
    }
}

void ink_to_dxf(const InkSheet& s, DxfDocument& doc) {
    ink_layer_to_dxf(s.visible, "VISIBLE", "CONTINUOUS", "CENTER", doc);
    ink_layer_to_dxf(s.hidden, "HIDDEN", "DASHED", "CENTER", doc);
    ink_layer_to_dxf(s.annotations, "DIMENSIONS", "CONTINUOUS", "CENTER", doc);
    ink_layer_to_dxf(s.hatch, "HATCH", "CONTINUOUS", nullptr, doc);
    ink_layer_to_dxf(s.frame, "BORDER", "CONTINUOUS", nullptr, doc);
    ink_layer_to_dxf(s.tables, "TABLES", "CONTINUOUS", nullptr, doc);
}

// Lay sheets left-to-right with a 20 mm gutter (DXF/DWG have no page concept) and write
// them as `fmt` ("dxf" or "dwg").
std::string write_drawing_dxf(const std::string& path,
                              const std::vector<const InkSheet*>& sheets,
                              const std::string& fmt) {
    DxfDocument doc;
    declare_drawing_layers(doc);
    double offset_x = 0.0, max_h = 0.0;
    bool fixed = true;
    for (const InkSheet* sheet : sheets) {
        double w = sheet->page_w;
        if (!sheet->fixed_page || w <= 0.0) {
            fixed = false;
            const InkBounds b = ink_bounds(*sheet);
            w = b.ok ? b.maxx : 0.0;
        }
        const std::size_t np = doc.polylines.size(), na = doc.arcs.size(),
                          ns = doc.solids.size(), nt = doc.texts.size();
        ink_to_dxf(*sheet, doc);
        if (offset_x != 0.0) {
            for (std::size_t i = np; i < doc.polylines.size(); ++i)
                for (auto& p : doc.polylines[i].pts) p[0] += offset_x;
            for (std::size_t i = na; i < doc.arcs.size(); ++i) doc.arcs[i].center[0] += offset_x;
            for (std::size_t i = ns; i < doc.solids.size(); ++i)
                for (auto& p : doc.solids[i].pts) p[0] += offset_x;
            for (std::size_t i = nt; i < doc.texts.size(); ++i) doc.texts[i].pos[0] += offset_x;
        }
        max_h = std::max(max_h, sheet->page_h);
        offset_x += w + 20.0;
    }
    if (fixed && !sheets.empty()) {
        doc.has_limits = true;
        doc.limits = {0.0, 0.0, offset_x - 20.0, max_h};
    }
    return fmt == "dwg" ? write_dwg(path, doc) : write_dxf(path, doc);
}

// Write one sheet (or the stateless pooled views) as dxf / dwg / pdf / svg.
std::string write_ink(const InkSheet& sheet, const std::string& path, const std::string& fmt) {
    if (fmt == "dxf" || fmt == "dwg") return write_drawing_dxf(path, {&sheet}, fmt);
    if (fmt == "svg") return write_svg(path, sheet);
    return write_pdf(path, sheet);
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

InkSheet pool_drawing_sheet(const json& sheet) {
    InkSheet ink;
    if (!sheet.is_object()) return ink;
    if (auto mm = sheet.find("size_mm");
        mm != sheet.end() && mm->is_array() && mm->size() == 2 && (*mm)[0].is_number() &&
        (*mm)[1].is_number()) {
        ink.page_w = (*mm)[0].get<double>();
        ink.page_h = (*mm)[1].get<double>();
        ink.fixed_page = ink.page_w > 20.0 && ink.page_h > 20.0;
    }
    if (auto fr = sheet.find("frame"); fr != sheet.end()) pool_draw(*fr, ink.frame);
    auto views = sheet.find("views");
    if (views == sheet.end() || !views->is_array()) return ink;
    for (const json& view : *views) {
        if (!view.is_object()) continue;
        InkLayer& ann =
            view.value("type", std::string()) == "table_overlay" ? ink.tables : ink.annotations;
        pool_key(view, "visible", ink.visible.thick);
        pool_key(view, "hidden", ink.hidden.dashed);
        pool_key(view, "hatch", ink.hatch.thin);
        if (auto d = view.find("draw"); d != view.end()) pool_draw(*d, ann);
        if (auto anns = view.find("annotations"); anns != view.end() && anns->is_array())
            for (const json& an : *anns) {
                if (!an.is_object()) continue;
                auto d = an.find("draw");
                if (d == an.end()) continue;
                pool_draw(*d, ann);
                ++ink.annotation_count;
            }
    }
    return ink;
}

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
    if (fmt != "dxf" && fmt != "dwg" && fmt != "pdf" && fmt != "svg")
        throw OpError("invalid_op", "format must be 'dxf', 'dwg', 'pdf', or 'svg'",
                      {{"format", fmt}});
    CompileResult res = compile_document(doc, env.is_null() ? json::object() : env);
    json projected = project_all(res.shapes, views);

    // Pool visible (thick) + hidden (dashed) polylines (already absolute sheet mm); no
    // sheet, so the writers size the page to the geometry.
    InkSheet ink;
    for (const auto& view : projected) {
        pool_key(view, "visible", ink.visible.thick);
        pool_key(view, "hidden", ink.hidden.dashed);
    }
    const int n_visible = static_cast<int>(ink.visible.thick.size());
    const int n_hidden = static_cast<int>(ink.hidden.dashed.size());
    int n = n_visible + n_hidden;
    if (n == 0)
        throw OpError("export_failed",
                      "export2d_drawing: the drawing has no view geometry to export",
                      {{"path", path}});

    std::string out_path;
    try {
        out_path = write_ink(ink, path, fmt);
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& exc) {
        throw OpError("export_failed", std::string("export2d_drawing: ") + exc.what(),
                      {{"path", path}, {"format", fmt}});
    }

    return {{"path", out_path}, {"format", fmt}, {"visible", n_visible},
            {"hidden", n_hidden}, {"entities", n}};
}

json export_drawing_selected(const std::string& doc_id, const std::string& path,
                             const std::string& fmt, const json& sheet_selector) {
    if (fmt != "dxf" && fmt != "dwg" && fmt != "pdf" && fmt != "svg")
        throw OpError("invalid_op", "format must be 'dxf', 'dwg', 'pdf', or 'svg'",
                      {{"format", fmt}});
    if (!sheet_selector.is_string())
        throw OpError("invalid_op",
                      "drawing_export sheet must be 'all' or an exact sheet id such as 's2'",
                      {{"reason", "bad sheet selector"}, {"sheet", sheet_selector}});
    json full = drawing_get(doc_id);
    const json& all_sheets = full.value("sheets", json::array());
    if (!all_sheets.is_array() || all_sheets.empty())
        throw OpError("export_failed", "export_drawing: drawing has no sheets",
                      {{"doc_id", doc_id}});
    std::string selector = sheet_selector.get<std::string>();
    if (selector.empty())
        selector = full.value("active_sheet_id", std::string("s1"));

    std::vector<const json*> selected;
    if (selector == "all") {
        for (const json& sheet : all_sheets) selected.push_back(&sheet);
    } else {
        for (const json& sheet : all_sheets)
            if (sheet.value("id", std::string()) == selector) selected.push_back(&sheet);
        if (selected.empty())
            throw OpError("invalid_op", "no sheet '" + selector + "'",
                          {{"reason", "unknown sheet"}, {"sheet", selector}});
    }

    struct SheetPage {
        std::string id;
        InkSheet ink;
    };
    std::vector<SheetPage> pages;
    pages.reserve(selected.size());
    int visible_count = 0, hidden_count = 0, annotation_count = 0;
    json sheet_ids = json::array();
    json sheet_sizes = json::array();
    for (const json* sheet : selected) {
        SheetPage page{sheet->value("id", std::string()), pool_drawing_sheet(*sheet)};
        visible_count += static_cast<int>(page.ink.visible.thick.size());
        hidden_count += static_cast<int>(page.ink.hidden.dashed.size());
        annotation_count += page.ink.annotation_count;
        sheet_ids.push_back(page.id);
        sheet_sizes.push_back(json::array({page.ink.page_w, page.ink.page_h}));
        pages.push_back(std::move(page));
    }

    std::string out_path;
    json out_paths = json::array();
    try {
        std::vector<const InkSheet*> inks;
        inks.reserve(pages.size());
        for (const SheetPage& page : pages) inks.push_back(&page.ink);
        if (fmt == "pdf") {
            out_path = write_pdf(path, inks);
        } else if (fmt == "svg") {
            const std::filesystem::path requested(path);
            for (std::size_t i = 0; i < pages.size(); ++i) {
                std::filesystem::path target = requested;
                if (pages.size() > 1) {
                    std::string stem =
                        requested.extension() == ".svg" ? requested.stem().string()
                                                         : requested.filename().string();
                    std::string sheet_id =
                        pages[i].id.empty() ? "s" + std::to_string(i + 1) : pages[i].id;
                    target = requested.parent_path() / (stem + "_" + sheet_id + ".svg");
                }
                out_paths.push_back(write_svg(target.string(), pages[i].ink));
            }
            out_path = out_paths.front().get<std::string>();
        } else {
            // DXF/DWG have no page concept: sheets are laid left-to-right with a 20 mm gutter.
            out_path = write_drawing_dxf(path, inks, fmt);
        }
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& exc) {
        throw OpError("export_failed", std::string("export_drawing: ") + exc.what(),
                      {{"doc_id", doc_id}, {"path", path}, {"format", fmt}});
    }
    if (out_paths.empty()) out_paths.push_back(out_path);

    json result = {{"path", out_path}, {"paths", out_paths}, {"format", fmt},
                   {"visible", visible_count}, {"hidden", hidden_count},
                   {"annotations", annotation_count},
                   {"entities", visible_count + hidden_count},
                   {"fixed_page", true}, {"page_count", pages.size()},
                   {"sheets", sheet_ids}, {"sheet_mm_all", sheet_sizes}};
    if (!pages.empty())
        result["sheet_mm"] =
            json::array({pages.front().ink.page_w, pages.front().ink.page_h});
    return result;
}

json export_drawing(const std::string& doc_id, const std::string& path,
                    const std::string& fmt) {
    return export_drawing_selected(doc_id, path, fmt, json("all"));
}

json export_flat_dxf(const std::string& doc_id, const std::string& path, const std::string& fmt) {
    if (fmt != "dxf" && fmt != "dwg" && fmt != "pdf")
        throw OpError("invalid_op", "format must be 'dxf', 'dwg', or 'pdf'", {{"format", fmt}});
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
        if (fmt == "dxf" || fmt == "dwg") {
            DxfDocument dxf;
            dxf.add_layer("OUTLINE", 7, "CONTINUOUS");
            dxf.add_layer("BEND", 3, "CONTINUOUS");
            dxf.add_layer("CUTS", 7, "CONTINUOUS");
            dxf.polylines.push_back({"OUTLINE", outline, 0.0, ""});
            for (const auto& s : bend_segs) dxf.polylines.push_back({"BEND", s, 0.0, ""});
            for (const auto& c : cuts) dxf.polylines.push_back({"CUTS", c, 0.0, ""});
            out_path = fmt == "dwg" ? write_dwg(path, dxf) : write_dxf(path, dxf);
        } else {
            // Outline + cut loops as thick outlines; bend lines thin dashed so they stay
            // visually distinct (PDF has no named layers). Page sized to the blank.
            InkSheet ink;
            ink.visible.thick.push_back(outline);
            for (const auto& c : cuts) ink.visible.thick.push_back(c);
            for (const auto& s : bend_segs) ink.annotations.dashed.push_back(s);
            out_path = write_pdf(path, ink);
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

}  // namespace chudware::doc
