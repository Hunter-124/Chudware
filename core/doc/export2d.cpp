// 2D drawing + export RPCs — see export2d.h.

#include "doc/export2d.h"
#include <array>
#include <functional>
#include <string>
#include <utility>
#include <vector>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>

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

namespace chudware::doc {

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

// Map an annotation string onto plain ASCII for DXF R12 / PDF Helvetica (the engine
// composes ⌀/×/°/± which neither target draws cleanly): ⌀->DIA, ×->X, °->DEG, ±->+-,
// other non-ASCII dropped. Mirrors session.cpp::sanitize_label for the export side (N-7).
std::string ascii_label(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (std::size_t i = 0; i < in.size();) {
        unsigned char c = static_cast<unsigned char>(in[i]);
        if (c < 0x80) { out.push_back(in[i]); ++i; continue; }
        if (c == 0xC3 && i + 1 < in.size()) {
            unsigned char d = static_cast<unsigned char>(in[i + 1]);
            if (d == 0x97) out += "X";
            else if (d == 0x98 || d == 0xB8) out += "DIA ";
            i += 2; continue;
        }
        if (c == 0xC2 && i + 1 < in.size()) {
            unsigned char d = static_cast<unsigned char>(in[i + 1]);
            if (d == 0xB0) out += " DEG";
            else if (d == 0xB1) out += "+-";
            i += 2; continue;
        }
        if (c == 0xE2 && i + 2 < in.size() &&
            static_cast<unsigned char>(in[i + 1]) == 0x8C &&
            static_cast<unsigned char>(in[i + 2]) == 0x80) {
            out += "DIA ";
            i += 3; continue;
        }
        ++i;
        while (i < in.size() && (static_cast<unsigned char>(in[i]) & 0xC0) == 0x80) ++i;
    }
    return out;
}

// A placed export label (pos, ascii text, height mm).
struct ExpLabel {
    std::array<double, 2> pos;
    std::string text;
    double h = 3.5;
};

// Drawing annotations pooled for vector export: dimension/leader/GD&T strokes, placed
// text, and the actual paper border/title block. All coordinates stay in sheet mm.
struct AnnExport {
    std::vector<Polyline> segs;       // solid strokes (dims, frames, arrows, border/title block)
    std::vector<Polyline> dashed;     // dashed strokes (center lines)
    std::vector<ExpLabel> labels;     // placed annotation + title-block text
    double minx = 1e30, miny = 1e30, maxx = -1e30, maxy = -1e30;  // geometry+ann bbox
    double page_w = 0.0, page_h = 0.0;
    bool fixed_page = false;
    int annotation_count = 0;
    bool any() const {
        return !segs.empty() || !dashed.empty() || !labels.empty();
    }
};

void add_line(AnnExport& ax, double x0, double y0, double x1, double y1) {
    ax.segs.push_back(Polyline{{x0, y0}, {x1, y1}});
}

void add_rect(AnnExport& ax, double x0, double y0, double x1, double y1) {
    ax.segs.push_back(Polyline{{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}, {x0, y0}});
}

std::string title_value(const json& tb, const char* first, const char* second = nullptr) {
    for (const char* key : {first, second}) {
        if (!key) continue;
        auto it = tb.find(key);
        if (it == tb.end()) continue;
        if (it->is_string()) return ascii_label(it->get<std::string>());
        if (it->is_number() || it->is_boolean()) return ascii_label(it->dump());
    }
    return "";
}

std::string clip_cell(std::string text, double width, double height) {
    const int chars = std::max(1, static_cast<int>(std::floor((width - 4.0) / (height * 0.6))));
    if (static_cast<int>(text.size()) <= chars) return text;
    if (chars <= 2) return text.substr(0, static_cast<std::size_t>(chars));
    return text.substr(0, static_cast<std::size_t>(chars - 2)) + "..";
}

void add_sheet_frame(const json& sheet, AnnExport& ax) {
    const json& mm = sheet.value("size_mm", json(nullptr));
    if (!mm.is_array() || mm.size() != 2 || !mm[0].is_number() || !mm[1].is_number())
        return;
    const double page_w = mm[0].get<double>(), page_h = mm[1].get<double>();
    if (page_w <= 20.0 || page_h <= 20.0) return;
    ax.page_w = page_w;
    ax.page_h = page_h;
    ax.fixed_page = true;

    constexpr double margin = 10.0;
    add_rect(ax, margin, margin, page_w - margin, page_h - margin);

    // Compact ASME/ISO-style grid in the lower-right corner. Ten standard fields occupy
    // five paired rows rather than the old unframed vertical text list below the geometry.
    const double block_w = std::min(180.0, page_w - 2.0 * margin);
    const double block_h = std::min(55.0, page_h - 2.0 * margin);
    const double x0 = page_w - margin - block_w, x1 = page_w - margin;
    const double y0 = margin, y1 = margin + block_h;
    const double mid = x0 + block_w * 0.5;
    constexpr int rows = 5;
    const double row_h = block_h / rows;
    add_rect(ax, x0, y0, x1, y1);
    add_line(ax, mid, y0, mid, y1);
    for (int row = 1; row < rows; ++row)
        add_line(ax, x0, y0 + row * row_h, x1, y0 + row * row_h);

    const json& tb = sheet.value("title_block", json::object());
    struct Cell {
        const char* label;
        const char* first;
        const char* second;
    };
    static const Cell cells[rows][2] = {
        {{"TITLE", "title", nullptr}, {"PART NO", "part_number", "part_no"}},
        {{"MATERIAL", "material", nullptr}, {"FINISH", "finish", nullptr}},
        {{"DRAWN BY", "drawn_by", "drawn"}, {"DATE", "date", nullptr}},
        {{"SCALE", "scale", nullptr}, {"REV", "rev", "revision"}},
        {{"SHEET", "sheet", nullptr}, {"COMPANY", "company", nullptr}},
    };
    const double cell_w = block_w * 0.5;
    for (int row = 0; row < rows; ++row) {
        // Row 0 is the top row, matching conventional title-block reading order.
        const double bottom = y1 - (row + 1) * row_h;
        for (int col = 0; col < 2; ++col) {
            const double left = x0 + col * cell_w;
            ax.labels.push_back({{left + 2.0, bottom + row_h - 3.0}, cells[row][col].label, 2.0});
            const std::string value = clip_cell(
                title_value(tb, cells[row][col].first, cells[row][col].second),
                cell_w, 3.0);
            if (!value.empty())
                ax.labels.push_back({{left + 2.0, bottom + 2.0}, value, 3.0});
        }
    }
}

// Tessellate one draw "arcs" entry into a polyline.
void arc_to_polyline(const json& arc, std::vector<Polyline>& out) {
    if (!arc.is_object() || !arc.contains("center")) return;
    double cx = arc["center"][0].get<double>(), cy = arc["center"][1].get<double>();
    double r = arc.value("radius", 0.0);
    double a0 = arc.value("a0", 0.0), a1 = arc.value("a1", 2.0 * 3.14159265358979323846);
    if (r <= 0.0) return;
    double span = std::fabs(a1 - a0);
    int n = std::max(8, (int)std::ceil(span / 0.35));
    Polyline pl;
    for (int i = 0; i <= n; ++i) {
        double t = a0 + (a1 - a0) * (double)i / (double)n;
        pl.push_back({cx + r * std::cos(t), cy + r * std::sin(t)});
    }
    if (pl.size() >= 2) out.push_back(std::move(pl));
}

// Pool one annotation's `draw` primitive block into the export pools.
void pool_draw_block(const json& draw, AnnExport& ax,
                     const std::function<void(const std::array<double, 2>&)>& grow) {
    if (!draw.is_object()) return;
    auto pool_polys = [&](const json& arr, std::vector<Polyline>& dst) {
        if (!arr.is_array()) return;
        for (const auto& pl : arr) {
            if (!pl.is_array() || pl.size() < 2) continue;
            Polyline out;
            for (const auto& p : pl) {
                if (!p.is_array() || p.size() < 2) continue;
                std::array<double, 2> q{p[0].get<double>(), p[1].get<double>()};
                out.push_back(q);
                grow(q);
            }
            if (out.size() >= 2) dst.push_back(std::move(out));
        }
    };
    pool_polys(draw.value("segments", json::array()), ax.segs);
    pool_polys(draw.value("dashed", json::array()), ax.dashed);
    // Arrowhead triangles -> outlined (DXF R12 has no robust fill); close the loop.
    if (draw.contains("tris") && draw["tris"].is_array())
        for (const auto& t : draw["tris"]) {
            if (!t.is_array() || t.size() != 3) continue;
            Polyline tri;
            for (const auto& p : t) tri.push_back({p[0].get<double>(), p[1].get<double>()});
            tri.push_back(tri.front());
            ax.segs.push_back(std::move(tri));
        }
    if (draw.contains("arcs") && draw["arcs"].is_array())
        for (const auto& a : draw["arcs"]) arc_to_polyline(a, ax.segs);
    if (draw.contains("texts") && draw["texts"].is_array())
        for (const auto& t : draw["texts"]) {
            if (!t.is_object() || !t.contains("pos")) continue;
            std::string txt = ascii_label(t.value("text", std::string()));
            if (txt.empty()) continue;
            double h = t.value("h", 3.5);
            std::array<double, 2> pos{t["pos"][0].get<double>(), t["pos"][1].get<double>()};
            // DXF/PDF text is left/baseline; nudge for center/right + mid so it lands close.
            std::string align = t.value("align", std::string("left"));
            std::string valign = t.value("valign", std::string("base"));
            double w = (double)txt.size() * h * 0.6;
            if (align == "center") pos[0] -= w * 0.5;
            else if (align == "right") pos[0] -= w;
            if (valign == "mid") pos[1] -= h * 0.5;
            ax.labels.push_back({pos, txt, h});
            grow(pos);
        }
}

// Pool one sheet's annotations, table overlays, section hatch, border, and title block.
void pool_sheet_annotations(const json& sheet, AnnExport& ax) {
    auto grow = [&](const std::array<double, 2>& p) {
        ax.minx = std::min(ax.minx, p[0]); ax.miny = std::min(ax.miny, p[1]);
        ax.maxx = std::max(ax.maxx, p[0]); ax.maxy = std::max(ax.maxy, p[1]);
    };
    for (const auto& view : sheet.value("views", json::array())) {
        for (const auto& an : view.value("annotations", json::array())) {
            if (!an.is_object()) continue;
            if (an.contains("draw")) {
                pool_draw_block(an["draw"], ax, grow);
                ++ax.annotation_count;
            }
        }
        if (view.contains("hatch") && view["hatch"].is_array())
            for (const auto& sgmt : view["hatch"]) {
                if (!sgmt.is_array() || sgmt.size() < 2) continue;
                Polyline pl;
                for (const auto& p : sgmt)
                    pl.push_back({p[0].get<double>(), p[1].get<double>()});
                if (pl.size() >= 2) ax.segs.push_back(std::move(pl));
            }
    }
    add_sheet_frame(sheet, ax);
}

// Write {VISIBLE, HIDDEN} polyline pools (+ optional annotations) to a DXF/PDF file.
// Shared writer for the stateless + stateful drawing exports. Returns the written path.
std::string write_drawing_layers(const std::vector<Polyline>& visible,
                                 const std::vector<Polyline>& hidden, const std::string& path,
                                 const std::string& fmt, const AnnExport& ax = AnnExport{}) {
    std::vector<ExpLabel> all_labels = ax.labels;
    if (fmt == "dxf") {
        DxfLayers layers;
        layers.add_layer("VISIBLE");  // insertion order VISIBLE then HIDDEN (Python dict order)
        layers.add_layer("HIDDEN");
        for (const auto& pl : visible) layers.add_polyline("VISIBLE", pl);
        for (const auto& pl : hidden) layers.add_polyline("HIDDEN", pl);
        if (ax.any()) {
            layers.add_layer("DIMENSIONS");
            for (const auto& s : ax.segs) layers.add_polyline("DIMENSIONS", s);
            for (const auto& s : ax.dashed) layers.add_polyline("DIMENSIONS", s);
            for (const auto& lb : all_labels) layers.add_text("DIMENSIONS", lb.pos, lb.h, lb.text);
        }
        return write_dxf(path, layers);
    }
    std::vector<PdfLine> lines;
    for (const auto& pl : visible) lines.push_back({pl, false});
    for (const auto& pl : hidden) lines.push_back({pl, true});
    for (const auto& s : ax.segs) lines.push_back({s, false});
    for (const auto& s : ax.dashed) lines.push_back({s, true});
    std::vector<PdfText> texts;
    for (const auto& lb : all_labels) texts.push_back({lb.pos, lb.h, lb.text});
    return write_pdf(path, lines, texts, !ax.fixed_page, ax.page_w, ax.page_h);
}

struct PdfPageData {
    std::vector<PdfLine> lines;
    std::vector<PdfText> texts;
    double width_mm = 0.0;
    double height_mm = 0.0;
};

std::string pdf_f3(double value) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.3f", value);
    return buf;
}

std::string pdf_literal(std::string text) {
    std::string out;
    out.reserve(text.size());
    for (char c : text) {
        if (c == '\\' || c == '(' || c == ')') out.push_back('\\');
        out.push_back(c);
    }
    return out;
}

std::string page_content(const PdfPageData& page) {
    constexpr double mm_to_pt = 72.0 / 25.4;
    std::string out = pdf_f3(0.35 * mm_to_pt) + " w\n";
    int last_dashed = -1;
    for (const PdfLine& line : page.lines) {
        if (line.pts.size() < 2) continue;
        const int dashed = line.dashed ? 1 : 0;
        if (dashed != last_dashed) {
            out += dashed ? ("[" + pdf_f3(2.5 * mm_to_pt) + " " +
                             pdf_f3(2.5 * mm_to_pt) + "] 0 d\n")
                          : "[] 0 d\n";
            last_dashed = dashed;
        }
        out += pdf_f3(line.pts[0][0] * mm_to_pt) + " " +
               pdf_f3(line.pts[0][1] * mm_to_pt) + " m\n";
        for (std::size_t i = 1; i < line.pts.size(); ++i)
            out += pdf_f3(line.pts[i][0] * mm_to_pt) + " " +
                   pdf_f3(line.pts[i][1] * mm_to_pt) + " l\n";
        out += "S\n";
    }
    if (!page.texts.empty()) {
        out += "0 0 0.6 rg\n";
        for (const PdfText& text : page.texts) {
            if (text.text.empty()) continue;
            out += "BT /F1 " + pdf_f3(text.height * mm_to_pt) + " Tf " +
                   pdf_f3(text.pos[0] * mm_to_pt) + " " +
                   pdf_f3(text.pos[1] * mm_to_pt) + " Td (" +
                   pdf_literal(text.text) + ") Tj ET\n";
        }
    }
    return out;
}

std::string write_multipage_pdf(const std::string& path,
                                const std::vector<PdfPageData>& pages) {
    if (pages.empty()) throw std::runtime_error("write_pdf: no pages to export");
    constexpr double mm_to_pt = 72.0 / 25.4;
    bool with_text = false;
    for (const auto& page : pages)
        if (!page.texts.empty()) with_text = true;
    const int page_count = static_cast<int>(pages.size());
    const int first_page_obj = 3;
    const int first_content_obj = first_page_obj + page_count;
    const int font_obj = first_content_obj + page_count;
    std::vector<std::string> objects;
    objects.push_back("<< /Type /Catalog /Pages 2 0 R >>");
    std::string kids = "<< /Type /Pages /Kids [";
    for (int i = 0; i < page_count; ++i)
        kids += std::to_string(first_page_obj + i) + " 0 R ";
    kids += "] /Count " + std::to_string(page_count) + " >>";
    objects.push_back(std::move(kids));
    for (int i = 0; i < page_count; ++i) {
        const PdfPageData& page = pages[static_cast<std::size_t>(i)];
        const std::string resources =
            with_text ? ("<< /Font << /F1 " + std::to_string(font_obj) + " 0 R >> >>")
                      : "<< >>";
        objects.push_back(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " +
            pdf_f3(page.width_mm * mm_to_pt) + " " +
            pdf_f3(page.height_mm * mm_to_pt) + "] /Contents " +
            std::to_string(first_content_obj + i) + " 0 R /Resources " +
            resources + " >>");
    }
    for (const PdfPageData& page : pages) {
        const std::string content = page_content(page);
        objects.push_back("<< /Length " + std::to_string(content.size()) +
                          " >>\nstream\n" + content + "endstream");
    }
    if (with_text)
        objects.push_back("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");

    std::string bytes = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n";
    std::vector<std::size_t> offsets;
    offsets.reserve(objects.size());
    for (std::size_t i = 0; i < objects.size(); ++i) {
        offsets.push_back(bytes.size());
        bytes += std::to_string(i + 1) + " 0 obj\n" + objects[i] + "\nendobj\n";
    }
    const std::size_t xref = bytes.size();
    bytes += "xref\n0 " + std::to_string(objects.size() + 1) +
             "\n0000000000 65535 f \n";
    for (std::size_t off : offsets) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%010zu 00000 n \n", off);
        bytes += buf;
    }
    bytes += "trailer\n<< /Size " + std::to_string(objects.size() + 1) +
             " /Root 1 0 R >>\nstartxref\n" + std::to_string(xref) + "\n%%EOF\n";

    std::filesystem::path target(path);
    if (target.has_parent_path()) std::filesystem::create_directories(target.parent_path());
    std::ofstream stream(target, std::ios::binary | std::ios::trunc);
    if (!stream) throw std::runtime_error("write_pdf: cannot open output: " + path);
    stream.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    stream.close();
    return std::filesystem::absolute(target).string();
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

json export_drawing_selected(const std::string& doc_id, const std::string& path,
                             const std::string& fmt, const json& sheet_selector) {
    if (fmt != "dxf" && fmt != "pdf")
        throw OpError("invalid_op", "format must be 'dxf' or 'pdf'", {{"format", fmt}});
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
        std::vector<Polyline> visible;
        std::vector<Polyline> hidden;
        AnnExport annotations;
    };
    std::vector<SheetPage> pages;
    pages.reserve(selected.size());
    int visible_count = 0, hidden_count = 0, annotation_count = 0;
    json sheet_ids = json::array();
    json sheet_sizes = json::array();
    for (const json* sheet : selected) {
        SheetPage page;
        page.id = sheet->value("id", std::string());
        for (const json& view : sheet->value("views", json::array())) {
            pool_polylines(view.value("visible", json::array()), page.visible);
            pool_polylines(view.value("hidden", json::array()), page.hidden);
        }
        for (const auto& pl : page.visible)
            for (const auto& p : pl) {
                page.annotations.minx = std::min(page.annotations.minx, p[0]);
                page.annotations.miny = std::min(page.annotations.miny, p[1]);
                page.annotations.maxx = std::max(page.annotations.maxx, p[0]);
                page.annotations.maxy = std::max(page.annotations.maxy, p[1]);
            }
        pool_sheet_annotations(*sheet, page.annotations);
        visible_count += static_cast<int>(page.visible.size());
        hidden_count += static_cast<int>(page.hidden.size());
        annotation_count += page.annotations.annotation_count;
        sheet_ids.push_back(page.id);
        sheet_sizes.push_back(
            json::array({page.annotations.page_w, page.annotations.page_h}));
        pages.push_back(std::move(page));
    }

    std::string out_path;
    try {
        if (fmt == "pdf") {
            std::vector<PdfPageData> pdf_pages;
            pdf_pages.reserve(pages.size());
            for (const SheetPage& page : pages) {
                PdfPageData pdf;
                pdf.width_mm = page.annotations.page_w;
                pdf.height_mm = page.annotations.page_h;
                for (const auto& line : page.visible) pdf.lines.push_back({line, false});
                for (const auto& line : page.hidden) pdf.lines.push_back({line, true});
                for (const auto& line : page.annotations.segs) pdf.lines.push_back({line, false});
                for (const auto& line : page.annotations.dashed) pdf.lines.push_back({line, true});
                for (const auto& label : page.annotations.labels)
                    pdf.texts.push_back({label.pos, label.h, label.text});
                pdf_pages.push_back(std::move(pdf));
            }
            out_path = write_multipage_pdf(path, pdf_pages);
        } else {
            // DXF has no page concept. Lay selected sheets left-to-right with a 20 mm
            // gutter, preserving each complete border/title/table instead of overlapping
            // every sheet at the origin.
            std::vector<Polyline> visible, hidden;
            AnnExport combined;
            double offset_x = 0.0;
            auto shifted = [](const Polyline& input, double dx) {
                Polyline out = input;
                for (auto& p : out) p[0] += dx;
                return out;
            };
            for (const SheetPage& page : pages) {
                for (const auto& line : page.visible)
                    visible.push_back(shifted(line, offset_x));
                for (const auto& line : page.hidden)
                    hidden.push_back(shifted(line, offset_x));
                for (const auto& line : page.annotations.segs)
                    combined.segs.push_back(shifted(line, offset_x));
                for (const auto& line : page.annotations.dashed)
                    combined.dashed.push_back(shifted(line, offset_x));
                for (const auto& label : page.annotations.labels) {
                    ExpLabel copy = label;
                    copy.pos[0] += offset_x;
                    combined.labels.push_back(std::move(copy));
                }
                combined.annotation_count += page.annotations.annotation_count;
                offset_x += page.annotations.page_w + 20.0;
            }
            out_path = write_drawing_layers(visible, hidden, path, fmt, combined);
        }
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& exc) {
        throw OpError("export_failed", std::string("export_drawing: ") + exc.what(),
                      {{"doc_id", doc_id}, {"path", path}, {"format", fmt}});
    }

    json result = {{"path", out_path}, {"format", fmt},
                   {"visible", visible_count}, {"hidden", hidden_count},
                   {"annotations", annotation_count},
                   {"entities", visible_count + hidden_count},
                   {"fixed_page", true}, {"page_count", pages.size()},
                   {"sheets", sheet_ids}, {"sheet_mm_all", sheet_sizes}};
    if (!pages.empty())
        result["sheet_mm"] =
            json::array({pages.front().annotations.page_w, pages.front().annotations.page_h});
    return result;
}

json export_drawing(const std::string& doc_id, const std::string& path,
                    const std::string& fmt) {
    return export_drawing_selected(doc_id, path, fmt, json("all"));
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

}  // namespace chudware::doc
