// Vector PDF writer — see pdfvec.h.

#include "doc/pdfvec.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>

namespace chudware::doc {

namespace {

constexpr double kMmToPt = 72.0 / 25.4;
constexpr double kMarginMm = 10.0;
constexpr double kPi = 3.14159265358979323846;
// Helvetica cap height (AFM CapHeight 718/1000 em): font size = cap height / 0.718.
constexpr double kCapHeightEm = 0.718;

// Helvetica AFM advance widths (1/1000 em) for WinAnsi codes 32..126.
constexpr short kAsciiWidths[95] = {
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,  // ' '../
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,  // 0..?
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, // @..O
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,  // P.._
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,  // `..o
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584};      // p..~
// Helvetica widths for WinAnsi 160..255 (identical to Latin-1 there).
constexpr short kLatin1Widths[96] = {
    278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333,  // A0
    400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611,  // B0
    667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, // C0
    722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,  // D0
    556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278,  // E0
    556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500}; // F0

int glyph_width(unsigned char code) {
    if (code >= 32 && code <= 126) return kAsciiWidths[code - 32];
    if (code >= 160) return kLatin1Widths[code - 160];
    switch (code) {
        case 0x80: return 556;   // Euro
        case 0x85: return 1000;  // ellipsis
        case 0x91: case 0x92: return 222;
        case 0x93: case 0x94: return 333;
        case 0x95: return 350;   // bullet
        case 0x96: return 556;   // en dash
        case 0x97: return 1000;  // em dash
        default: return 556;
    }
}

// UTF-8 → WinAnsiEncoding bytes per the drawing contract (° ± × Ø native, ⌀ → Ø,
// ⌴/⌵/↧ → CBORE/CSK/DEPTH words). Unmappable code points become '?'.
std::string to_winansi(const std::string& utf8) {
    const std::string in = ink_expand_callouts(utf8);
    std::string out;
    out.reserve(in.size());
    for (std::size_t i = 0; i < in.size();) {
        const char32_t cp = ink_utf8_next(in, i);
        if (cp == '\t') { out.push_back(' '); continue; }
        if (cp >= 0x20 && cp <= 0x7E) { out.push_back(static_cast<char>(cp)); continue; }
        if (cp >= 0xA0 && cp <= 0xFF) { out.push_back(static_cast<char>(cp)); continue; }
        unsigned char code = '?';
        switch (cp) {
            case 0x2300: code = 0xD8; break;  // ⌀ diameter sign → Ø
            case 0x2212: code = '-'; break;   // minus
            case 0x2013: code = 0x96; break;
            case 0x2014: code = 0x97; break;
            case 0x2018: code = 0x91; break;
            case 0x2019: code = 0x92; break;
            case 0x201C: code = 0x93; break;
            case 0x201D: code = 0x94; break;
            case 0x2022: code = 0x95; break;
            case 0x2026: code = 0x85; break;
            case 0x20AC: code = 0x80; break;
            default: if (cp < 0x20) continue; break;
        }
        out.push_back(static_cast<char>(code));
    }
    return out;
}

double winansi_width_em(const std::string& bytes) {
    long sum = 0;
    for (unsigned char c : bytes) sum += glyph_width(c);
    return static_cast<double>(sum) / 1000.0;
}

// Compact fixed-point number: %.3f with trailing zeros stripped ("-0" → "0").
void num(std::string& out, double v) {
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%.3f", v);
    std::size_t n = std::strlen(buf);
    while (n > 0 && buf[n - 1] == '0') --n;
    if (n > 0 && buf[n - 1] == '.') --n;
    buf[n] = '\0';
    if (std::strcmp(buf, "-0") == 0 || n == 0) out += '0';
    else out.append(buf, n);
}

std::string f3(double v) {
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%.3f", v);
    return buf;
}

void pt(std::string& out, const InkPoint& p, const char* op) {
    num(out, p[0]);
    out += ' ';
    num(out, p[1]);
    out += op;
}

void polylines(std::string& out, const std::vector<InkPolyline>& pls) {
    for (const InkPolyline& pl : pls) {
        if (pl.size() < 2) continue;
        pt(out, pl[0], " m\n");
        for (std::size_t i = 1; i < pl.size(); ++i) pt(out, pl[i], " l\n");
    }
}

// Cubic Bézier approximation of a CCW arc, ≤ 90° per segment.
void arc_path(std::string& out, const InkArc& a) {
    if (a.radius <= 0.0) return;
    const bool full = a.full();
    const double span = a.sweep();
    if (std::fabs(span) < 1e-9) return;
    const int n = std::max(1, static_cast<int>(std::ceil(std::fabs(span) / (kPi / 2.0) - 1e-9)));
    const double d = span / n;
    const double k = 4.0 / 3.0 * std::tan(d / 4.0) * a.radius;
    const double cx = a.center[0], cy = a.center[1], r = a.radius;
    double t0 = a.a0;
    pt(out, {cx + r * std::cos(t0), cy + r * std::sin(t0)}, " m\n");
    for (int i = 0; i < n; ++i) {
        const double t1 = t0 + d;
        const InkPoint p0{cx + r * std::cos(t0), cy + r * std::sin(t0)};
        const InkPoint p3{cx + r * std::cos(t1), cy + r * std::sin(t1)};
        pt(out, {p0[0] - k * std::sin(t0), p0[1] + k * std::cos(t0)}, " ");
        pt(out, {p3[0] + k * std::sin(t1), p3[1] - k * std::cos(t1)}, " ");
        pt(out, p3, " c\n");
        t0 = t1;
    }
    if (full) out += "h\n";
}

std::string literal(const std::string& bytes) {
    std::string o;
    o.reserve(bytes.size() + 2);
    for (unsigned char c : bytes) {
        if (c == '\\' || c == '(' || c == ')') {
            o.push_back('\\');
            o.push_back(static_cast<char>(c));
        } else if (c >= 0x80) {
            char buf[8];
            std::snprintf(buf, sizeof(buf), "\\%03o", c);
            o += buf;
        } else {
            o.push_back(static_cast<char>(c));
        }
    }
    return o;
}

struct Stroke {
    double width;
    const char* dash;
};

// One page content stream in sheet-mm user space.
std::string page_content(const InkSheet& s, double dx_mm, double dy_mm) {
    std::string out;
    out.reserve(1 << 16);
    out += "q\n";
    num(out, kMmToPt); out += " 0 0 "; num(out, kMmToPt); out += ' ';
    num(out, dx_mm * kMmToPt); out += ' '; num(out, dy_mm * kMmToPt);
    out += " cm\n1 J 1 j 0 G 0 g\n";

    std::string dash_arr = "[";
    num(dash_arr, kInkDash[0]); dash_arr += ' '; num(dash_arr, kInkDash[1]);
    dash_arr += "] 0 d\n";
    std::string chain_arr = "[";
    for (int i = 0; i < 4; ++i) {
        if (i) chain_arr += ' ';
        num(chain_arr, kInkChain[i]);
    }
    chain_arr += "] 0 d\n";

    auto stroke_class = [&](double width, const std::string& dash, auto&& emit) {
        std::string body;
        emit(body);
        if (body.empty()) return;
        num(out, width);
        out += " w\n";
        out += dash;
        out += body;
        out += "S\n";
    };
    const std::string solid = "[] 0 d\n";
    stroke_class(kInkThin, solid, [&](std::string& b) {
        s.for_each_layer([&](const InkLayer& l) {
            polylines(b, l.thin);
            for (const InkArc& a : l.arcs) arc_path(b, a);
        });
    });
    stroke_class(kInkThin, dash_arr, [&](std::string& b) {
        s.for_each_layer([&](const InkLayer& l) { polylines(b, l.dashed); });
    });
    stroke_class(kInkThin, chain_arr, [&](std::string& b) {
        s.for_each_layer([&](const InkLayer& l) { polylines(b, l.chain); });
    });
    stroke_class(kInkThick, solid, [&](std::string& b) {
        s.for_each_layer([&](const InkLayer& l) { polylines(b, l.thick); });
    });
    stroke_class(kInkThick, chain_arr, [&](std::string& b) {
        s.for_each_layer([&](const InkLayer& l) { polylines(b, l.chain_thick); });
    });

    // Filled arrowheads.
    std::string fills;
    s.for_each_layer([&](const InkLayer& l) {
        for (const auto& t : l.tris) {
            pt(fills, t[0], " m\n");
            pt(fills, t[1], " l\n");
            pt(fills, t[2], " l\nh\n");
        }
    });
    if (!fills.empty()) out += fills + "f\n";

    // Text: black Helvetica, positioned from AFM widths.
    s.for_each_layer([&](const InkLayer& l) {
        for (const InkText& t : l.texts) {
            if (t.text.empty() || t.h <= 0.0) continue;
            const std::string bytes = to_winansi(t.text);
            if (bytes.empty()) continue;
            const double size = t.h / kCapHeightEm;
            const double w = winansi_width_em(bytes) * size;
            double x = t.pos[0];
            if (t.align == InkAlign::center) x -= 0.5 * w;
            else if (t.align == InkAlign::right) x -= w;
            out += "BT /F1 ";
            num(out, size);
            out += " Tf ";
            pt(out, {x, t.baseline()}, " Td (");
            out += literal(bytes);
            out += ") Tj ET\n";
        }
    });
    out += "Q\n";
    return out;
}

}  // namespace

double pdf_text_width(const std::string& text, double cap_h) {
    return winansi_width_em(to_winansi(text)) * cap_h / kCapHeightEm;
}

std::string build_pdf(const std::vector<const InkSheet*>& pages) {
    if (pages.empty()) throw std::runtime_error("write_pdf: no pages to export");
    struct Page {
        double w_mm, h_mm, dx, dy;
    };
    std::vector<Page> geo;
    geo.reserve(pages.size());
    for (const InkSheet* s : pages) {
        if (!s) throw std::runtime_error("write_pdf: null page");
        if (s->fixed_page && s->page_w > 0.0 && s->page_h > 0.0) {
            geo.push_back({s->page_w, s->page_h, 0.0, 0.0});
            continue;
        }
        const InkBounds b = ink_bounds(*s);
        if (!b.ok) throw std::runtime_error("write_pdf: no line geometry to export");
        geo.push_back({b.maxx - b.minx + 2.0 * kMarginMm, b.maxy - b.miny + 2.0 * kMarginMm,
                       kMarginMm - b.minx, kMarginMm - b.miny});
    }

    const int n = static_cast<int>(pages.size());
    const int first_page_obj = 3;
    const int first_content_obj = first_page_obj + n;
    const int font_obj = first_content_obj + n;
    std::vector<std::string> objects;
    objects.push_back("<< /Type /Catalog /Pages 2 0 R >>");
    std::string kids = "<< /Type /Pages /Kids [";
    for (int i = 0; i < n; ++i) kids += std::to_string(first_page_obj + i) + " 0 R ";
    kids += "] /Count " + std::to_string(n) + " >>";
    objects.push_back(std::move(kids));
    const std::string resources =
        "<< /Font << /F1 " + std::to_string(font_obj) + " 0 R >> >>";
    for (int i = 0; i < n; ++i) {
        const Page& g = geo[static_cast<std::size_t>(i)];
        objects.push_back("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " +
                          f3(g.w_mm * kMmToPt) + " " + f3(g.h_mm * kMmToPt) +
                          "] /Contents " + std::to_string(first_content_obj + i) +
                          " 0 R /Resources " + resources + " >>");
    }
    for (int i = 0; i < n; ++i) {
        const Page& g = geo[static_cast<std::size_t>(i)];
        const std::string content =
            page_content(*pages[static_cast<std::size_t>(i)], g.dx, g.dy);
        objects.push_back("<< /Length " + std::to_string(content.size()) + " >>\nstream\n" +
                          content + "endstream");
    }
    objects.push_back(
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>");

    std::string buf = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n";
    std::vector<std::size_t> offsets;
    offsets.reserve(objects.size());
    for (std::size_t i = 0; i < objects.size(); ++i) {
        offsets.push_back(buf.size());
        buf += std::to_string(i + 1) + " 0 obj\n" + objects[i] + "\nendobj\n";
    }
    const std::size_t xref = buf.size();
    buf += "xref\n0 " + std::to_string(objects.size() + 1) + "\n0000000000 65535 f \n";
    for (std::size_t off : offsets) {
        char line[32];
        std::snprintf(line, sizeof(line), "%010zu 00000 n \n", off);
        buf += line;
    }
    buf += "trailer\n<< /Size " + std::to_string(objects.size() + 1) +
           " /Root 1 0 R >>\nstartxref\n" + std::to_string(xref) + "\n%%EOF\n";
    return buf;
}

std::string write_pdf(const std::string& path, const std::vector<const InkSheet*>& pages) {
    const std::string bytes = build_pdf(pages);
    std::filesystem::path p(path);
    if (p.has_parent_path()) std::filesystem::create_directories(p.parent_path());
    std::ofstream f(p, std::ios::binary | std::ios::trunc);
    if (!f) throw std::runtime_error("write_pdf: cannot open output: " + path);
    f.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    f.close();
    if (!f) throw std::runtime_error("write_pdf: failed writing output: " + path);
    return std::filesystem::absolute(p).string();
}

std::string write_pdf(const std::string& path, const InkSheet& sheet) {
    return write_pdf(path, std::vector<const InkSheet*>{&sheet});
}

}  // namespace chudware::doc
