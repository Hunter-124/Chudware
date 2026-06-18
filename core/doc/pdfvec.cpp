// Vector PDF writer — see pdfvec.h. Byte-faithful port of engine/doc/pdfvec.py.

#include "doc/pdfvec.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>

namespace aicad::doc {

namespace {

constexpr double MM_TO_PT = 72.0 / 25.4;
constexpr double MARGIN_MM = 10.0;

// f"{value:.3f}" — fixed 3-decimal formatting (Python's default for these ops).
std::string f3(double value) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.3f", value);
    return std::string(buf);
}

struct Bbox {
    bool ok = false;
    double minx = 0, miny = 0, maxx = 0, maxy = 0;
};

Bbox bbox_of(const std::vector<PdfLine>& lines) {
    Bbox b;
    double minx = std::numeric_limits<double>::infinity();
    double miny = std::numeric_limits<double>::infinity();
    double maxx = -std::numeric_limits<double>::infinity();
    double maxy = -std::numeric_limits<double>::infinity();
    for (const PdfLine& ln : lines) {
        for (const auto& p : ln.pts) {
            b.ok = true;
            if (p[0] < minx) minx = p[0];
            if (p[1] < miny) miny = p[1];
            if (p[0] > maxx) maxx = p[0];
            if (p[1] > maxy) maxy = p[1];
        }
    }
    if (b.ok) {
        b.minx = minx;
        b.miny = miny;
        b.maxx = maxx;
        b.maxy = maxy;
    }
    return b;
}

// Build the content stream (latin-1 bytes): stroke each polyline, dashed when
// flagged. Mirrors _content_stream exactly (op order + formatting + "\n" joins).
std::string content_stream(const std::vector<PdfLine>& lines, double dx_mm,
                           double dy_mm, int* path_count) {
    std::vector<std::string> ops;
    ops.push_back(f3(0.35 * MM_TO_PT) + " w");
    // last_dashed tri-state: -1 = None, 0 = false, 1 = true (matches Python's None).
    int last_dashed = -1;
    int n = 0;
    for (const PdfLine& ln : lines) {
        if (ln.pts.size() < 2) continue;
        int dashed = ln.dashed ? 1 : 0;
        if (dashed != last_dashed) {
            if (dashed) {
                double d = 2.5 * MM_TO_PT;
                ops.push_back("[" + f3(d) + " " + f3(d) + "] 0 d");
            } else {
                ops.push_back("[] 0 d");
            }
            last_dashed = dashed;
        }
        double x0 = (ln.pts[0][0] + dx_mm) * MM_TO_PT;
        double y0 = (ln.pts[0][1] + dy_mm) * MM_TO_PT;
        ops.push_back(f3(x0) + " " + f3(y0) + " m");
        for (std::size_t i = 1; i < ln.pts.size(); ++i) {
            double x = (ln.pts[i][0] + dx_mm) * MM_TO_PT;
            double y = (ln.pts[i][1] + dy_mm) * MM_TO_PT;
            ops.push_back(f3(x) + " " + f3(y) + " l");
        }
        ops.push_back("S");
        ++n;
    }
    if (path_count) *path_count = n;
    std::string s;
    for (std::size_t i = 0; i < ops.size(); ++i) {
        s += ops[i];
        s += "\n";
    }
    return s;
}

}  // namespace

std::string build_pdf(const std::vector<PdfLine>& lines, bool auto_page,
                      double page_w_mm_in, double page_h_mm_in, int* path_count) {
    Bbox bb = bbox_of(lines);
    if (!bb.ok) throw std::runtime_error("write_pdf: no line geometry to export");

    double dx_mm = MARGIN_MM - bb.minx;
    double dy_mm = MARGIN_MM - bb.miny;
    double page_w_mm, page_h_mm;
    if (!auto_page) {
        page_w_mm = page_w_mm_in;
        page_h_mm = page_h_mm_in;
        dx_mm = 0.0;
        dy_mm = 0.0;
    } else {
        page_w_mm = (bb.maxx - bb.minx) + 2 * MARGIN_MM;
        page_h_mm = (bb.maxy - bb.miny) + 2 * MARGIN_MM;
    }
    double page_w_pt = page_w_mm * MM_TO_PT;
    double page_h_pt = page_h_mm * MM_TO_PT;

    std::string content = content_stream(lines, dx_mm, dy_mm, path_count);

    // Assemble the PDF objects (byte-identical to pdfvec.py).
    std::vector<std::string> objects;
    objects.push_back("<< /Type /Catalog /Pages 2 0 R >>");
    objects.push_back("<< /Type /Pages /Kids [3 0 R] /Count 1 >>");
    objects.push_back("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + f3(page_w_pt) +
                      " " + f3(page_h_pt) + "] /Contents 4 0 R /Resources << >> >>");
    objects.push_back("<< /Length " + std::to_string(content.size()) + " >>\nstream\n" +
                      content + "endstream");

    std::string buf;
    buf += "%PDF-1.4\n";
    buf += "%\xe2\xe3\xcf\xd3\n";  // binary marker
    std::vector<std::size_t> offsets;
    for (std::size_t i = 0; i < objects.size(); ++i) {
        offsets.push_back(buf.size());
        buf += std::to_string(i + 1) + " 0 obj\n";
        buf += objects[i];
        buf += "\nendobj\n";
    }

    std::size_t xref_pos = buf.size();
    std::size_t n = objects.size();
    buf += "xref\n0 " + std::to_string(n + 1) + "\n";
    buf += "0000000000 65535 f \n";
    for (std::size_t off : offsets) {
        char obuf[32];
        std::snprintf(obuf, sizeof(obuf), "%010zu 00000 n \n", off);
        buf += obuf;
    }
    buf += "trailer\n";
    buf += "<< /Size " + std::to_string(n + 1) + " /Root 1 0 R >>\n";
    buf += "startxref\n";
    buf += std::to_string(xref_pos) + "\n";
    buf += "%%EOF\n";

    return buf;
}

std::string write_pdf(const std::string& path, const std::vector<PdfLine>& lines,
                      bool auto_page, double page_w_mm, double page_h_mm) {
    std::filesystem::path p(path);
    if (p.has_parent_path()) std::filesystem::create_directories(p.parent_path());

    int paths = 0;
    std::string buf = build_pdf(lines, auto_page, page_w_mm, page_h_mm, &paths);

    std::ofstream f(p, std::ios::binary | std::ios::trunc);
    if (!f) throw std::runtime_error("write_pdf: cannot open output: " + path);
    f.write(buf.data(), static_cast<std::streamsize>(buf.size()));
    f.close();
    return std::filesystem::absolute(p).string();
}

}  // namespace aicad::doc
