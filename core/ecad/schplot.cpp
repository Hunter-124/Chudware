#include "ecad/schplot.h"

#include "ecad/ops.h"
#include "ecad/schema.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <limits>
#include <locale>
#include <map>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace chudware::ecad::plot {
namespace {

using json = nlohmann::json;

constexpr double kPi = 3.14159265358979323846;
constexpr double kPtPerMm = 72.0 / 25.4;

void require_svg_sheet(const std::string& name, bool allow_empty = false) {
    if (name.empty() && allow_empty) return;
    if (name.empty() || name == "." || name == ".." ||
        name.find_first_of("/\\:*?\"<>|") != std::string::npos ||
        name.back() == '.' || name.back() == ' ' ||
        std::any_of(name.begin(), name.end(), [](unsigned char c) { return c < 32 || c == 127; }))
        throw OpError("invalid_op", "ecad_export: SVG sheet must be a safe filename leaf",
                      {{"reason", "unsafe SVG leaf"}});
}

struct Pt {
    double x = 0.0;
    double y = 0.0;
};

struct Color {
    double r;
    double g;
    double b;
    const char* svg;
};

constexpr Color kInk{0.08, 0.11, 0.17, "#151c2b"};
constexpr Color kWire{0.08, 0.29, 0.41, "#154a68"};
constexpr Color kBus{0.43, 0.25, 0.08, "#6e4014"};
constexpr Color kNet{0.02, 0.38, 0.43, "#05616e"};
constexpr Color kPower{0.66, 0.22, 0.07, "#a83812"};
constexpr Color kMuted{0.47, 0.49, 0.53, "#787d87"};
constexpr Color kFrame{0.17, 0.19, 0.23, "#2b303b"};
constexpr Color kWhite{1.0, 1.0, 1.0, "#ffffff"};

struct Pen {
    Color color = kInk;
    double width = 0.25;
    std::vector<double> dash;
};

enum class Align { Left, Center, Right };

std::string fmt(double v, int precision = 3) {
    if (std::fabs(v) < 0.0000005) v = 0.0;
    std::ostringstream os;
    os.imbue(std::locale::classic());
    os << std::fixed << std::setprecision(precision) << v;
    std::string s = os.str();
    while (s.size() > 1 && s.back() == '0') s.pop_back();
    if (!s.empty() && s.back() == '.') s.pop_back();
    return s.empty() ? "0" : s;
}

std::string svg_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        switch (c) {
            case '&': out += "&amp;"; break;
            case '<': out += "&lt;"; break;
            case '>': out += "&gt;"; break;
            case '"': out += "&quot;"; break;
            default: out += c; break;
        }
    }
    return out;
}

std::string pdf_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (unsigned char c : s) {
        if (c == '(' || c == ')' || c == '\\') out.push_back('\\');
        if (c == '\r') out += "\\r";
        else if (c == '\n') out += "\\n";
        else if (c < 32 || c > 126) out.push_back('?');
        else out.push_back(static_cast<char>(c));
    }
    return out;
}

bool xy(const json& v, Pt& p) {
    if (!v.is_array() || v.size() < 2 || !v[0].is_number() || !v[1].is_number()) return false;
    p = {v[0].get<double>(), v[1].get<double>()};
    return std::isfinite(p.x) && std::isfinite(p.y);
}

Pt point_or(const json& obj, const char* key, Pt fallback = {}) {
    Pt p;
    return obj.is_object() && obj.contains(key) && xy(obj[key], p) ? p : fallback;
}

double number_or(const json& obj, const char* key, double fallback) {
    if (!obj.is_object() || !obj.contains(key) || !obj[key].is_number()) return fallback;
    const double v = obj[key].get<double>();
    return std::isfinite(v) ? v : fallback;
}

std::string string_or(const json& obj, const char* key, const std::string& fallback = {}) {
    return obj.is_object() && obj.contains(key) && obj[key].is_string()
               ? obj[key].get<std::string>()
               : fallback;
}

std::vector<Pt> points_of(const json& obj, const char* key = "pts") {
    std::vector<Pt> out;
    if (!obj.is_object() || !obj.contains(key) || !obj[key].is_array()) return out;
    for (const json& v : obj[key]) {
        Pt p;
        if (xy(v, p)) out.push_back(p);
    }
    return out;
}

std::string graphic_kind(const json& g) {
    std::string kind = string_or(g, "kind");
    if (kind.empty()) kind = string_or(g, "type");
    return kind;
}

std::vector<Pt> graphic_points(const json& g) {
    std::vector<Pt> pts = points_of(g);
    if (!pts.empty()) return pts;
    Pt p;
    if (g.contains("p1") && xy(g["p1"], p)) pts.push_back(p);
    if (g.contains("p2") && xy(g["p2"], p)) pts.push_back(p);
    if (pts.empty() && g.contains("center") && xy(g["center"], p)) pts.push_back(p);
    if (pts.empty() && g.contains("pos") && xy(g["pos"], p)) pts.push_back(p);
    return pts;
}

bool bool_or(const json& obj, const char* key, bool fallback) {
    return obj.is_object() && obj.contains(key) && obj[key].is_boolean()
               ? obj[key].get<bool>()
               : fallback;
}

class Painter {
public:
    virtual ~Painter() = default;
    virtual void line(Pt a, Pt b, const Pen& pen) = 0;
    virtual void polyline(const std::vector<Pt>& pts, bool closed, const Pen& pen) = 0;
    virtual void rect(Pt top_left, double width, double height, const Pen& pen) = 0;
    virtual void circle(Pt center, double radius, const Pen& pen) = 0;
    virtual void arc(Pt center, double radius, double start_deg, double sweep_deg,
                     const Pen& pen) = 0;
    virtual void text(Pt at, const std::string& value, double size_mm, double angle_deg,
                      Align align, const Color& color) = 0;
    virtual void fill(const std::vector<Pt>& polygon, const Color& color) = 0;
};

class SvgPainter final : public Painter {
public:
    SvgPainter(double width, double height) : width_(width), height_(height) {
        out_.imbue(std::locale::classic());
        out_ << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
             << "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"" << fmt(width)
             << "mm\" height=\"" << fmt(height) << "mm\" viewBox=\"0 0 " << fmt(width)
             << ' ' << fmt(height) << "\">\n"
             << "<rect x=\"0\" y=\"0\" width=\"" << fmt(width) << "\" height=\""
             << fmt(height) << "\" fill=\"#ffffff\"/>\n"
             << "<g stroke-linecap=\"round\" stroke-linejoin=\"round\">\n";
    }

    void line(Pt a, Pt b, const Pen& pen) override {
        out_ << "<line x1=\"" << fmt(a.x) << "\" y1=\"" << fmt(a.y) << "\" x2=\""
             << fmt(b.x) << "\" y2=\"" << fmt(b.y) << "\"";
        stroke(pen);
        out_ << "/>\n";
    }

    void polyline(const std::vector<Pt>& pts, bool closed, const Pen& pen) override {
        if (pts.size() < 2) return;
        out_ << '<' << (closed ? "polygon" : "polyline") << " points=\"";
        for (std::size_t i = 0; i < pts.size(); ++i) {
            if (i) out_ << ' ';
            out_ << fmt(pts[i].x) << ',' << fmt(pts[i].y);
        }
        out_ << "\"";
        stroke(pen);
        out_ << "/>\n";
    }

    void rect(Pt p, double width, double height, const Pen& pen) override {
        out_ << "<rect x=\"" << fmt(p.x) << "\" y=\"" << fmt(p.y) << "\" width=\""
             << fmt(width) << "\" height=\"" << fmt(height) << "\"";
        stroke(pen);
        out_ << "/>\n";
    }

    void circle(Pt c, double radius, const Pen& pen) override {
        if (radius <= 0.0) return;
        out_ << "<circle cx=\"" << fmt(c.x) << "\" cy=\"" << fmt(c.y) << "\" r=\""
             << fmt(radius) << "\"";
        stroke(pen);
        out_ << "/>\n";
    }

    void arc(Pt c, double radius, double start_deg, double sweep_deg, const Pen& pen) override {
        if (radius <= 0.0 || std::fabs(sweep_deg) < 0.000001) return;
        const double a0 = start_deg * kPi / 180.0;
        const double a1 = (start_deg + sweep_deg) * kPi / 180.0;
        Pt p0{c.x + radius * std::cos(a0), c.y + radius * std::sin(a0)};
        Pt p1{c.x + radius * std::cos(a1), c.y + radius * std::sin(a1)};
        const int large = std::fabs(sweep_deg) > 180.0 ? 1 : 0;
        const int sweep = sweep_deg >= 0.0 ? 1 : 0;
        out_ << "<path d=\"M " << fmt(p0.x) << ' ' << fmt(p0.y) << " A " << fmt(radius)
             << ' ' << fmt(radius) << " 0 " << large << ' ' << sweep << ' ' << fmt(p1.x) << ' '
             << fmt(p1.y) << "\"";
        stroke(pen);
        out_ << "/>\n";
    }

    void text(Pt at, const std::string& value, double size_mm, double angle_deg, Align align,
              const Color& color) override {
        if (value.empty() || size_mm <= 0.0) return;
        const char* anchor = align == Align::Center ? "middle" : (align == Align::Right ? "end" : "start");
        out_ << "<text x=\"" << fmt(at.x) << "\" y=\"" << fmt(at.y)
             << "\" font-family=\"Helvetica,Arial,sans-serif\" font-size=\"" << fmt(size_mm)
             << "\" fill=\"" << color.svg << "\" stroke=\"none\" text-anchor=\"" << anchor
             << "\" dominant-baseline=\"central\"";
        if (std::fabs(angle_deg) > 0.000001)
            out_ << " transform=\"rotate(" << fmt(angle_deg) << ' ' << fmt(at.x) << ' '
                 << fmt(at.y) << ")\"";
        out_ << '>' << svg_escape(value) << "</text>\n";
    }

    void fill(const std::vector<Pt>& polygon, const Color& color) override {
        if (polygon.size() < 3) return;
        out_ << "<polygon points=\"";
        for (std::size_t i = 0; i < polygon.size(); ++i) {
            if (i) out_ << ' ';
            out_ << fmt(polygon[i].x) << ',' << fmt(polygon[i].y);
        }
        out_ << "\" fill=\"" << color.svg << "\" stroke=\"none\"/>\n";
    }

    std::string finish() {
        out_ << "</g>\n</svg>\n";
        return out_.str();
    }

private:
    void stroke(const Pen& pen) {
        out_ << " fill=\"none\" stroke=\"" << pen.color.svg << "\" stroke-width=\""
             << fmt(pen.width) << "\" vector-effect=\"none\"";
        if (!pen.dash.empty()) {
            out_ << " stroke-dasharray=\"";
            for (std::size_t i = 0; i < pen.dash.size(); ++i) {
                if (i) out_ << ',';
                out_ << fmt(pen.dash[i]);
            }
            out_ << '"';
        }
    }

    double width_;
    double height_;
    std::ostringstream out_;
};

class PdfPainter final : public Painter {
public:
    explicit PdfPainter(double height_mm) : height_(height_mm) { out_.imbue(std::locale::classic()); }

    void line(Pt a, Pt b, const Pen& pen) override {
        set_stroke(pen);
        move(a);
        line_to(b);
        out_ << "S\n";
    }

    void polyline(const std::vector<Pt>& pts, bool closed, const Pen& pen) override {
        if (pts.size() < 2) return;
        set_stroke(pen);
        move(pts.front());
        for (std::size_t i = 1; i < pts.size(); ++i) line_to(pts[i]);
        if (closed) out_ << "h\n";
        out_ << "S\n";
    }

    void rect(Pt p, double width, double height, const Pen& pen) override {
        set_stroke(pen);
        out_ << fmt(p.x * kPtPerMm) << ' ' << fmt((height_ - p.y - height) * kPtPerMm) << ' '
             << fmt(width * kPtPerMm) << ' ' << fmt(height * kPtPerMm) << " re S\n";
    }

    void circle(Pt c, double radius, const Pen& pen) override {
        if (radius <= 0.0) return;
        set_stroke(pen);
        ellipse_path(c, radius);
        out_ << "S\n";
    }

    void arc(Pt c, double radius, double start_deg, double sweep_deg, const Pen& pen) override {
        if (radius <= 0.0 || std::fabs(sweep_deg) < 0.000001) return;
        set_stroke(pen);
        const int pieces = std::max(1, static_cast<int>(std::ceil(std::fabs(sweep_deg) / 90.0)));
        const double step = sweep_deg / pieces;
        double a = start_deg * kPi / 180.0;
        Pt start{c.x + radius * std::cos(a), c.y + radius * std::sin(a)};
        move(start);
        for (int i = 0; i < pieces; ++i) {
            const double b = a + step * kPi / 180.0;
            const double k = 4.0 / 3.0 * std::tan((b - a) / 4.0);
            Pt p0{c.x + radius * std::cos(a), c.y + radius * std::sin(a)};
            Pt p3{c.x + radius * std::cos(b), c.y + radius * std::sin(b)};
            Pt p1{p0.x - radius * k * std::sin(a), p0.y + radius * k * std::cos(a)};
            Pt p2{p3.x + radius * k * std::sin(b), p3.y - radius * k * std::cos(b)};
            curve(p1, p2, p3);
            a = b;
        }
        out_ << "S\n";
    }

    void text(Pt at, const std::string& value, double size_mm, double angle_deg, Align align,
              const Color& color) override {
        if (value.empty() || size_mm <= 0.0) return;
        double x = at.x;
        const double estimated = size_mm * 0.52 * static_cast<double>(value.size());
        if (align == Align::Center) x -= estimated * 0.5;
        else if (align == Align::Right) x -= estimated;
        const double y = at.y + size_mm * 0.34;
        const double a = angle_deg * kPi / 180.0;
        const double c = std::cos(a), s = std::sin(a);
        out_ << fmt(color.r) << ' ' << fmt(color.g) << ' ' << fmt(color.b) << " rg\nBT\n/F1 "
             << fmt(size_mm * kPtPerMm) << " Tf\n" << fmt(c) << ' ' << fmt(-s) << ' '
             << fmt(s) << ' ' << fmt(c) << ' ' << fmt(x * kPtPerMm) << ' '
             << fmt((height_ - y) * kPtPerMm) << " Tm\n(" << pdf_escape(value) << ") Tj\nET\n";
    }

    void fill(const std::vector<Pt>& polygon, const Color& color) override {
        if (polygon.size() < 3) return;
        out_ << fmt(color.r) << ' ' << fmt(color.g) << ' ' << fmt(color.b) << " rg\n";
        move(polygon.front());
        for (std::size_t i = 1; i < polygon.size(); ++i) line_to(polygon[i]);
        out_ << "h f\n";
    }

    std::string finish() const { return out_.str(); }

private:
    void set_stroke(const Pen& pen) {
        out_ << fmt(pen.color.r) << ' ' << fmt(pen.color.g) << ' ' << fmt(pen.color.b) << " RG\n"
             << fmt(pen.width * kPtPerMm) << " w\n";
        if (pen.dash.empty()) out_ << "[] 0 d\n";
        else {
            out_ << '[';
            for (std::size_t i = 0; i < pen.dash.size(); ++i) {
                if (i) out_ << ' ';
                out_ << fmt(pen.dash[i] * kPtPerMm);
            }
            out_ << "] 0 d\n";
        }
    }

    Pt pdf(Pt p) const { return {p.x * kPtPerMm, (height_ - p.y) * kPtPerMm}; }

    void move(Pt p) {
        p = pdf(p);
        out_ << fmt(p.x) << ' ' << fmt(p.y) << " m\n";
    }

    void line_to(Pt p) {
        p = pdf(p);
        out_ << fmt(p.x) << ' ' << fmt(p.y) << " l\n";
    }

    void curve(Pt a, Pt b, Pt c) {
        a = pdf(a);
        b = pdf(b);
        c = pdf(c);
        out_ << fmt(a.x) << ' ' << fmt(a.y) << ' ' << fmt(b.x) << ' ' << fmt(b.y) << ' '
             << fmt(c.x) << ' ' << fmt(c.y) << " c\n";
    }

    void ellipse_path(Pt c, double r) {
        constexpr double k = 0.5522847498307936;
        move({c.x + r, c.y});
        curve({c.x + r, c.y + k * r}, {c.x + k * r, c.y + r}, {c.x, c.y + r});
        curve({c.x - k * r, c.y + r}, {c.x - r, c.y + k * r}, {c.x - r, c.y});
        curve({c.x - r, c.y - k * r}, {c.x - k * r, c.y - r}, {c.x, c.y - r});
        curve({c.x + k * r, c.y - r}, {c.x + r, c.y - k * r}, {c.x + r, c.y});
        out_ << "h\n";
    }

    double height_;
    std::ostringstream out_;
};

struct Bounds {
    double min_x = std::numeric_limits<double>::infinity();
    double min_y = std::numeric_limits<double>::infinity();
    double max_x = -std::numeric_limits<double>::infinity();
    double max_y = -std::numeric_limits<double>::infinity();

    void add(Pt p, double pad = 0.0) {
        if (!std::isfinite(p.x) || !std::isfinite(p.y)) return;
        min_x = std::min(min_x, p.x - pad);
        min_y = std::min(min_y, p.y - pad);
        max_x = std::max(max_x, p.x + pad);
        max_y = std::max(max_y, p.y + pad);
    }

    bool valid() const { return min_x <= max_x && min_y <= max_y; }
    double width() const { return valid() ? max_x - min_x : 0.0; }
    double height() const { return valid() ? max_y - min_y : 0.0; }
};

Pt rotate(Pt p, double degrees) {
    const double a = degrees * kPi / 180.0;
    const double c = std::cos(a), s = std::sin(a);
    return {p.x * c - p.y * s, p.x * s + p.y * c};
}

Pt local_to_world(Pt p, const json& symbol) {
    const std::string mirror = string_or(symbol, "mirror", "none");
    if (mirror == "x") p.x = -p.x;
    else if (mirror == "y") p.y = -p.y;
    p = rotate(p, number_or(symbol, "angle", 0.0));
    const Pt pos = point_or(symbol, "pos");
    return {p.x + pos.x, p.y + pos.y};
}

Pt pin_root_world(const json& symbol, const json& pin) {
    json root_pin = pin;
    root_pin["length"] = 0.0;
    const auto p = schema::pin_world(symbol, root_pin);
    return {p.first, p.second};
}

Pt pin_tip_world(const json& symbol, const json& pin) {
    const auto p = schema::pin_world(symbol, pin);
    return {p.first, p.second};
}

struct Mapper {
    double scale = 1.0;
    Pt world_center;
    Pt page_center;

    Pt operator()(Pt p) const {
        return {page_center.x + (p.x - world_center.x) * scale,
                page_center.y - (p.y - world_center.y) * scale};
    }

    double length(double mm) const { return mm * scale; }
};

struct SheetPage {
    std::string id;
    std::string name;
    int index = 0;
    int total = 1;
};

std::vector<SheetPage> selected_sheets(const json& sch, const PlotOpts& o) {
    std::vector<SheetPage> all;
    if (sch.is_object() && sch.contains("sheets") && sch["sheets"].is_array()) {
        const int total = static_cast<int>(sch["sheets"].size());
        for (int i = 0; i < total; ++i) {
            const json& sh = sch["sheets"][static_cast<std::size_t>(i)];
            if (!sh.is_object()) continue;
            const std::string id = string_or(sh, "id", "sh" + std::to_string(i + 1));
            all.push_back({id, string_or(sh, "name", id), i, std::max(total, 1)});
        }
    }
    if (all.empty()) all.push_back({"sh1", "root", 0, 1});
    if (o.sheet.empty()) return all;
    for (const SheetPage& sh : all)
        if (sh.id == o.sheet) return {sh};
    return {{o.sheet, o.sheet, 0, static_cast<int>(all.size())}};
}

bool on_sheet(const json& item, const SheetPage& sheet, const std::string& first_sheet) {
    if (!item.is_object()) return false;
    if (!item.contains("sheet") || !item["sheet"].is_string()) return sheet.id == first_sheet;
    return item["sheet"].get<std::string>() == sheet.id;
}

std::vector<const json*> items_on_sheet(const json& sch, const char* key, const SheetPage& sheet,
                                        const std::string& first_sheet) {
    std::vector<const json*> out;
    if (!sch.is_object() || !sch.contains(key) || !sch[key].is_array()) return out;
    for (const json& item : sch[key])
        if (on_sheet(item, sheet, first_sheet)) out.push_back(&item);
    std::stable_sort(out.begin(), out.end(), [](const json* a, const json* b) {
        return string_or(*a, "id") < string_or(*b, "id");
    });
    return out;
}

const json* symbol_def(const json& sch, const json& symbol) {
    const std::string libid = string_or(symbol, "libid");
    if (libid.empty() || !sch.is_object() || !sch.contains("lib_cache") ||
        !sch["lib_cache"].is_object())
        return nullptr;
    auto it = sch["lib_cache"].find(libid);
    return it != sch["lib_cache"].end() && it->is_object() ? &(*it) : nullptr;
}

struct ArcGeom {
    Pt center;
    double radius = 0.0;
    double start_deg = 0.0;
    double sweep_deg = 0.0;
    bool valid = false;
};

double positive_degrees(double degrees) {
    degrees = std::fmod(degrees, 360.0);
    return degrees < 0.0 ? degrees + 360.0 : degrees;
}

ArcGeom arc_geometry(const json& g) {
    const std::vector<Pt> pts = graphic_points(g);
    const double radius = std::fabs(number_or(g, "radius", 0.0));
    if (!pts.empty() && radius > 0.0) {
        return {pts.front(), radius, number_or(g, "a0", 0.0),
                number_or(g, "a1", 90.0) - number_or(g, "a0", 0.0), true};
    }

    Pt start, middle, end;
    const bool have_start = g.contains("p1") && xy(g["p1"], start);
    const bool have_end = g.contains("p2") && xy(g["p2"], end);
    const bool have_middle = g.contains("mid") && xy(g["mid"], middle);
    if (!have_start || !have_end) return {};
    if (!have_middle) {
        const Pt center{(start.x + end.x) * 0.5, (start.y + end.y) * 0.5};
        const double r = std::hypot(end.x - start.x, end.y - start.y) * 0.5;
        if (r <= 0.0) return {};
        const double a0 = std::atan2(start.y - center.y, start.x - center.x) * 180.0 / kPi;
        return {center, r, a0, 180.0, true};
    }

    const double d = 2.0 * (start.x * (middle.y - end.y) +
                            middle.x * (end.y - start.y) +
                            end.x * (start.y - middle.y));
    if (std::fabs(d) < 1e-12) return {};
    const double s2 = start.x * start.x + start.y * start.y;
    const double m2 = middle.x * middle.x + middle.y * middle.y;
    const double e2 = end.x * end.x + end.y * end.y;
    const Pt center{(s2 * (middle.y - end.y) + m2 * (end.y - start.y) +
                     e2 * (start.y - middle.y)) /
                        d,
                    (s2 * (end.x - middle.x) + m2 * (start.x - end.x) +
                     e2 * (middle.x - start.x)) /
                        d};
    const double r = std::hypot(start.x - center.x, start.y - center.y);
    if (r <= 0.0 || !std::isfinite(r)) return {};
    const double a0 = std::atan2(start.y - center.y, start.x - center.x) * 180.0 / kPi;
    const double am = std::atan2(middle.y - center.y, middle.x - center.x) * 180.0 / kPi;
    const double a1 = std::atan2(end.y - center.y, end.x - center.x) * 180.0 / kPi;
    const double ccw = positive_degrees(a1 - a0);
    const double through_middle = positive_degrees(am - a0);
    const double sweep = through_middle <= ccw + 1e-7 ? ccw : ccw - 360.0;
    return {center, r, a0, sweep, true};
}

void add_graphic_bounds(Bounds& b, const json& g, const json* symbol = nullptr) {
    const std::string kind = graphic_kind(g);
    std::vector<Pt> pts = graphic_points(g);
    auto add = [&](Pt p, double pad = 0.0) {
        b.add(symbol ? local_to_world(p, *symbol) : p, pad);
    };
    for (Pt p : pts) add(p, number_or(g, "width", 0.25) * 0.5);
    if (kind == "circle" && !pts.empty()) {
        const double r = std::fabs(number_or(g, "radius", 0.0));
        Pt c = symbol ? local_to_world(pts.front(), *symbol) : pts.front();
        b.add(c, r);
    } else if (kind == "arc") {
        const ArcGeom arc = arc_geometry(g);
        if (arc.valid) {
            Pt c = symbol ? local_to_world(arc.center, *symbol) : arc.center;
            b.add(c, arc.radius);
        }
    }
    if (kind == "text" && !pts.empty()) b.add(symbol ? local_to_world(pts.front(), *symbol) : pts.front(), 3.0);
}

Bounds content_bounds(const json& sch, const SheetPage& sheet, const std::string& first_sheet) {
    Bounds b;
    for (const char* key : {"graphics", "buses", "wires"})
        for (const json* item : items_on_sheet(sch, key, sheet, first_sheet)) {
            if (std::string(key) == "graphics") add_graphic_bounds(b, *item);
            else for (Pt p : points_of(*item)) b.add(p, 0.8);
        }
    for (const json* item : items_on_sheet(sch, "bus_entries", sheet, first_sheet)) {
        const Pt p = point_or(*item, "pos");
        const Pt d = point_or(*item, "size", {2.54, 2.54});
        b.add(p, 0.5);
        b.add({p.x + d.x, p.y + d.y}, 0.5);
    }
    for (const char* key : {"junctions", "no_connect", "labels", "power", "texts"})
        for (const json* item : items_on_sheet(sch, key, sheet, first_sheet))
            b.add(point_or(*item, "pos"), std::string(key) == "labels" ? 8.0 : 3.0);
    for (const json* sp : items_on_sheet(sch, "symbols", sheet, first_sheet)) {
        const json* def = symbol_def(sch, *sp);
        if (def && def->contains("graphics") && (*def)["graphics"].is_array())
            for (const json& g : (*def)["graphics"]) add_graphic_bounds(b, g, sp);
        if (def && def->contains("pins") && (*def)["pins"].is_array()) {
            const int unit = static_cast<int>(number_or(*sp, "unit", 1));
            for (const json& pin : (*def)["pins"]) {
                if (!pin.is_object() || static_cast<int>(number_or(pin, "unit", 1)) != unit) continue;
                b.add(pin_root_world(*sp, pin), 1.0);
                b.add(pin_tip_world(*sp, pin), 2.0);
            }
        }
        b.add(point_or(*sp, "pos"), 6.0);
    }
    for (const json* hs : items_on_sheet(sch, "sheet_symbols", sheet, first_sheet)) {
        const Pt pos = point_or(*hs, "pos");
        Pt size = point_or(*hs, "size", {32.0, 20.0});
        b.add({pos.x - std::fabs(size.x) * 0.5, pos.y - std::fabs(size.y) * 0.5}, 2.0);
        b.add({pos.x + std::fabs(size.x) * 0.5, pos.y + std::fabs(size.y) * 0.5}, 2.0);
        if (hs->contains("pins") && (*hs)["pins"].is_array())
            for (const json& pin : (*hs)["pins"]) b.add(point_or(pin, "pos"), 3.0);
    }
    return b;
}

std::vector<Pt> circle_polygon(Pt c, double radius, int segments = 32) {
    std::vector<Pt> out;
    out.reserve(static_cast<std::size_t>(segments));
    for (int i = 0; i < segments; ++i) {
        const double a = 2.0 * kPi * i / segments;
        out.push_back({c.x + radius * std::cos(a), c.y + radius * std::sin(a)});
    }
    return out;
}

void draw_filled_circle(Painter& p, Pt center, double radius, const Color& color) {
    p.fill(circle_polygon(center, radius), color);
}

void draw_graphic(Painter& p, const json& g, const Mapper& map, const Color& color,
                  const json* symbol = nullptr) {
    const std::string kind = graphic_kind(g);
    std::vector<Pt> local = graphic_points(g);
    if (local.empty()) return;
    auto world = [&](Pt q) { return symbol ? local_to_world(q, *symbol) : q; };
    auto page = [&](Pt q) { return map(world(q)); };
    const Pen pen{color, std::max(0.08, map.length(number_or(g, "width", 0.25))), {}};
    const bool filled = bool_or(g, "filled", false);

    if (kind == "line") {
        if (local.size() >= 2) p.line(page(local[0]), page(local[1]), pen);
    } else if (kind == "rect") {
        if (local.size() < 2) return;
        std::vector<Pt> corners{{local[0].x, local[0].y}, {local[1].x, local[0].y},
                                {local[1].x, local[1].y}, {local[0].x, local[1].y}};
        for (Pt& q : corners) q = page(q);
        if (filled) p.fill(corners, color);
        p.polyline(corners, true, pen);
    } else if (kind == "circle") {
        const double r = map.length(std::fabs(number_or(g, "radius", 0.0)));
        const Pt c = page(local.front());
        if (filled) p.fill(circle_polygon(c, r), color);
        p.circle(c, r, pen);
    } else if (kind == "arc") {
        const ArcGeom arc = arc_geometry(g);
        if (!arc.valid) return;
        const double r = map.length(arc.radius);
        const Pt center = page(arc.center);
        const Pt start_local{arc.center.x + arc.radius * std::cos(arc.start_deg * kPi / 180.0),
                             arc.center.y + arc.radius * std::sin(arc.start_deg * kPi / 180.0)};
        const Pt start = page(start_local);
        const double page_start =
            std::atan2(start.y - center.y, start.x - center.x) * 180.0 / kPi;
        double det = 1.0;
        if (symbol && (string_or(*symbol, "mirror", "none") == "x" ||
                       string_or(*symbol, "mirror", "none") == "y"))
            det = -1.0;
        const double sweep = -det * arc.sweep_deg;
        if (filled) {
            std::vector<Pt> sector{center};
            const int n = std::max(4, static_cast<int>(std::ceil(std::fabs(sweep) / 10.0)));
            for (int i = 0; i <= n; ++i) {
                const double a = (page_start + sweep * i / n) * kPi / 180.0;
                sector.push_back({center.x + r * std::cos(a), center.y + r * std::sin(a)});
            }
            p.fill(sector, color);
        }
        p.arc(center, r, page_start, sweep, pen);
    } else if (kind == "polyline") {
        std::vector<Pt> pts;
        pts.reserve(local.size());
        for (Pt q : local) pts.push_back(page(q));
        if (filled && pts.size() >= 3) p.fill(pts, color);
        p.polyline(pts, filled, pen);
    } else if (kind == "text") {
        const double size = map.length(number_or(g, "size", 1.27));
        const double local_angle = number_or(g, "angle", 0.0);
        const Pt origin = page(local.front());
        const Pt axis = page({local.front().x + std::cos(local_angle * kPi / 180.0),
                              local.front().y + std::sin(local_angle * kPi / 180.0)});
        const double angle = std::atan2(axis.y - origin.y, axis.x - origin.x) * 180.0 / kPi;
        p.text(origin, string_or(g, "text"), size, angle, Align::Left, color);
    }
}

void draw_frame(Painter& p, double width, double height, double margin, bool enabled) {
    if (!enabled) return;
    const Pen outer{kFrame, 0.45, {}};
    const Pen inner{kFrame, 0.25, {}};
    p.rect({0.8, 0.8}, width - 1.6, height - 1.6, outer);
    p.rect({margin, margin}, width - 2.0 * margin, height - 2.0 * margin, inner);

    const int cols = std::max(1, static_cast<int>(std::round((width - 2.0 * margin) / 45.0)));
    const int rows = std::max(1, static_cast<int>(std::round((height - 2.0 * margin) / 40.0)));
    for (int i = 0; i <= cols; ++i) {
        const double x = margin + (width - 2.0 * margin) * i / cols;
        p.line({x, 0.8}, {x, margin}, inner);
        p.line({x, height - margin}, {x, height - 0.8}, inner);
        if (i < cols) {
            std::string label(1, static_cast<char>('A' + (i % 26)));
            const double cx = margin + (width - 2.0 * margin) * (i + 0.5) / cols;
            p.text({cx, margin * 0.5}, label, 2.5, 0.0, Align::Center, kFrame);
            p.text({cx, height - margin * 0.5}, label, 2.5, 0.0, Align::Center, kFrame);
        }
    }
    for (int i = 0; i <= rows; ++i) {
        const double y = margin + (height - 2.0 * margin) * i / rows;
        p.line({0.8, y}, {margin, y}, inner);
        p.line({width - margin, y}, {width - 0.8, y}, inner);
        if (i < rows) {
            const std::string label = std::to_string(i + 1);
            const double cy = margin + (height - 2.0 * margin) * (i + 0.5) / rows;
            p.text({margin * 0.5, cy}, label, 2.5, 0.0, Align::Center, kFrame);
            p.text({width - margin * 0.5, cy}, label, 2.5, 0.0, Align::Center, kFrame);
        }
    }
}

std::string title_value(const json& sch, const char* key, const std::string& fallback = {}) {
    if (sch.is_object() && sch.contains("title_block") && sch["title_block"].is_object()) {
        const json& tb = sch["title_block"];
        if (tb.contains(key) && tb[key].is_string()) return tb[key].get<std::string>();
    }
    return fallback;
}

void draw_title_block(Painter& p, const json& sch, const SheetPage& sheet, double page_w,
                      double page_h, double margin, double scale, bool enabled) {
    if (!enabled) return;
    const double w = std::min(96.0, std::max(55.0, page_w - 2.0 * margin));
    const double h = 31.0;
    const double x = page_w - margin - w;
    const double y = page_h - margin - h;
    const Pen pen{kFrame, 0.25, {}};
    p.fill({{x, y}, {x + w, y}, {x + w, y + h}, {x, y + h}}, kWhite);
    p.rect({x, y}, w, h, pen);
    p.line({x, y + 12.0}, {x + w, y + 12.0}, pen);
    p.line({x, y + 21.0}, {x + w, y + 21.0}, pen);
    p.line({x + w * 0.62, y + 21.0}, {x + w * 0.62, y + h}, pen);
    p.line({x + w * 0.79, y + 21.0}, {x + w * 0.79, y + h}, pen);

    const std::string doc_name = string_or(sch, "name", "Schematic");
    const std::string title = title_value(sch, "title", doc_name);
    const std::string company = title_value(sch, "company");
    const std::string rev = title_value(sch, "rev", title_value(sch, "revision"));
    const std::string date = title_value(sch, "date");
    p.text({x + 2.0, y + 4.1}, title, 3.4, 0.0, Align::Left, kInk);
    p.text({x + 2.0, y + 9.0}, company, 2.0, 0.0, Align::Left, kInk);
    p.text({x + 2.0, y + 16.1}, "Document: " + doc_name, 2.1, 0.0, Align::Left, kInk);
    p.text({x + 2.0, y + 19.4}, "Sheet name: " + sheet.name, 1.8, 0.0, Align::Left, kInk);
    p.text({x + 2.0, y + 24.7}, "Date: " + date, 1.8, 0.0, Align::Left, kInk);
    p.text({x + 2.0, y + 28.7}, scale < 0.99995 ? "Scale: " + fmt(scale, 4) + " (fit)" : "Scale: 1:1",
           1.8, 0.0, Align::Left, kInk);
    p.text({x + w * 0.705, y + 24.7}, "Rev", 1.5, 0.0, Align::Center, kInk);
    p.text({x + w * 0.705, y + 28.5}, rev, 2.1, 0.0, Align::Center, kInk);
    p.text({x + w * 0.895, y + 24.7}, "Sheet", 1.5, 0.0, Align::Center, kInk);
    p.text({x + w * 0.895, y + 28.5}, std::to_string(sheet.index + 1) + "/" + std::to_string(sheet.total),
           2.1, 0.0, Align::Center, kInk);
}

void draw_bus_and_wires(Painter& p, const json& sch, const SheetPage& sheet,
                        const std::string& first_sheet, const Mapper& map) {
    for (const json* bus : items_on_sheet(sch, "buses", sheet, first_sheet)) {
        std::vector<Pt> pts = points_of(*bus);
        for (Pt& q : pts) q = map(q);
        p.polyline(pts, false, {kBus, std::max(0.65, map.length(0.65)), {}});
        if (!pts.empty()) {
            std::string name = string_or(*bus, "name");
            if (name.empty() && bus->contains("members") && (*bus)["members"].is_array()) {
                bool first = true;
                for (const json& m : (*bus)["members"])
                    if (m.is_string()) {
                        if (!first) name += ',';
                        name += m.get<std::string>();
                        first = false;
                    }
            }
            p.text({pts.front().x + 1.0, pts.front().y - 2.0}, name, 1.8, 0.0, Align::Left, kBus);
        }
    }
    for (const json* entry : items_on_sheet(sch, "bus_entries", sheet, first_sheet)) {
        const Pt a = point_or(*entry, "pos");
        const Pt d = point_or(*entry, "size", {2.54, 2.54});
        p.line(map(a), map({a.x + d.x, a.y + d.y}), {kBus, std::max(0.35, map.length(0.4)), {}});
    }
    for (const json* wire : items_on_sheet(sch, "wires", sheet, first_sheet)) {
        std::vector<Pt> pts = points_of(*wire);
        for (Pt& q : pts) q = map(q);
        p.polyline(pts, false, {kWire, std::max(0.2, map.length(0.25)), {}});
    }
}

void draw_junctions_and_nc(Painter& p, const json& sch, const SheetPage& sheet,
                           const std::string& first_sheet, const Mapper& map) {
    for (const json* junction : items_on_sheet(sch, "junctions", sheet, first_sheet))
        draw_filled_circle(p, map(point_or(*junction, "pos")), std::max(0.42, map.length(0.48)), kWire);
    for (const json* nc : items_on_sheet(sch, "no_connect", sheet, first_sheet)) {
        const Pt q = map(point_or(*nc, "pos"));
        const double r = std::max(0.75, map.length(1.0));
        const Pen pen{kPower, std::max(0.22, map.length(0.25)), {}};
        p.line({q.x - r, q.y - r}, {q.x + r, q.y + r}, pen);
        p.line({q.x - r, q.y + r}, {q.x + r, q.y - r}, pen);
    }
}

bool pins_hidden(const json& def, bool numbers) {
    const std::array<const char*, 2> hidden_keys = numbers
        ? std::array<const char*, 2>{"hide_pin_numbers", "pin_numbers_hidden"}
        : std::array<const char*, 2>{"hide_pin_names", "pin_names_hidden"};
    for (const char* key : hidden_keys)
        if (bool_or(def, key, false)) return true;
    const char* show = numbers ? "show_pin_numbers" : "show_pin_names";
    return def.contains(show) && def[show].is_boolean() && !def[show].get<bool>();
}

void hatch_symbol(Painter& p, const Bounds& box, const Mapper& map) {
    if (!box.valid()) return;
    Pt a = map({box.min_x, box.max_y});
    Pt b = map({box.max_x, box.min_y});
    const double left = std::min(a.x, b.x), right = std::max(a.x, b.x);
    const double top = std::min(a.y, b.y), bottom = std::max(a.y, b.y);
    const Pen pen{kMuted, 0.18, {0.8, 0.8}};
    const double span = bottom - top;
    for (double x = left - span; x <= right; x += 2.0) {
        double x0 = x, y0 = bottom, x1 = x + span, y1 = top;
        if (x0 < left) { y0 -= left - x0; x0 = left; }
        if (x1 > right) { y1 += x1 - right; x1 = right; }
        if (y0 >= top && y1 <= bottom) p.line({x0, y0}, {x1, y1}, pen);
    }
}

void draw_symbols(Painter& p, const json& sch, const SheetPage& sheet,
                  const std::string& first_sheet, const Mapper& map, const PlotOpts& opts) {
    for (const json* symbol : items_on_sheet(sch, "symbols", sheet, first_sheet)) {
        const json* def = symbol_def(sch, *symbol);
        const bool dnp = bool_or(*symbol, "dnp", false);
        const Color body_color = dnp ? kMuted : (def && bool_or(*def, "power", false) ? kPower : kInk);
        Bounds symbol_box;
        if (def && def->contains("graphics") && (*def)["graphics"].is_array())
            for (const json& g : (*def)["graphics"]) {
                add_graphic_bounds(symbol_box, g, symbol);
                draw_graphic(p, g, map, body_color, symbol);
            }
        if (dnp) hatch_symbol(p, symbol_box, map);

        if (def && def->contains("pins") && (*def)["pins"].is_array()) {
            const int unit = static_cast<int>(number_or(*symbol, "unit", 1));
            std::vector<const json*> pins;
            for (const json& pin : (*def)["pins"])
                if (pin.is_object() && static_cast<int>(number_or(pin, "unit", 1)) == unit)
                    pins.push_back(&pin);
            std::stable_sort(pins.begin(), pins.end(), [](const json* a, const json* b) {
                return string_or(*a, "number") < string_or(*b, "number");
            });
            for (const json* pin : pins) {
                const Pt root_w = pin_root_world(*symbol, *pin);
                const Pt tip_w = pin_tip_world(*symbol, *pin);
                const Pt root = map(root_w), tip = map(tip_w);
                const Pen pin_pen{dnp ? kMuted : kBus, std::max(0.18, map.length(0.22)), {}};
                p.line(root, tip, pin_pen);
                double dx = tip.x - root.x, dy = tip.y - root.y;
                double len = std::hypot(dx, dy);
                if (len < 0.000001) { dx = 1.0; dy = 0.0; len = 1.0; }
                dx /= len;
                dy /= len;
                const double nx = -dy, ny = dx;
                const std::string shape = string_or(*pin, "shape", "line");
                if (shape.find("inverted") != std::string::npos) {
                    const double r = std::max(0.38, map.length(0.5));
                    p.circle(tip, r, pin_pen);
                }
                if (shape.find("clock") != std::string::npos) {
                    const double depth = std::max(0.9, map.length(1.0));
                    const double half = std::max(0.55, map.length(0.65));
                    const std::vector<Pt> tri{{root.x, root.y},
                                              {root.x - dx * depth + nx * half,
                                               root.y - dy * depth + ny * half},
                                              {root.x - dx * depth - nx * half,
                                               root.y - dy * depth - ny * half}};
                    p.polyline(tri, true, pin_pen);
                }
                if (opts.pin_numbers && !pins_hidden(*def, true)) {
                    Pt at{tip.x - dx * std::max(0.7, map.length(0.8)) + nx * 0.75,
                          tip.y - dy * std::max(0.7, map.length(0.8)) + ny * 0.75};
                    p.text(at, string_or(*pin, "number"), std::max(1.05, map.length(1.15)), 0.0,
                           Align::Center, body_color);
                }
                if (opts.pin_names && !pins_hidden(*def, false)) {
                    Pt at{root.x - dx * std::max(1.0, map.length(1.1)) + nx * 0.75,
                          root.y - dy * std::max(1.0, map.length(1.1)) + ny * 0.75};
                    p.text(at, string_or(*pin, "name"), std::max(1.05, map.length(1.15)), 0.0,
                           Align::Center, body_color);
                }
            }
        }

        const Pt pos = map(point_or(*symbol, "pos"));
        p.text({pos.x + 2.0, pos.y - 4.0}, string_or(*symbol, "ref"), 1.8, 0.0, Align::Left,
               dnp ? kMuted : kNet);
        p.text({pos.x + 2.0, pos.y + 2.0}, string_or(*symbol, "value"), 1.8, 0.0, Align::Left,
               body_color);
    }
}

std::string uppercase(std::string s) {
    for (char& c : s)
        if (c >= 'a' && c <= 'z') c = static_cast<char>(c - 'a' + 'A');
    return s;
}

void draw_power(Painter& p, const json& sch, const SheetPage& sheet,
                const std::string& first_sheet, const Mapper& map) {
    for (const json* power : items_on_sheet(sch, "power", sheet, first_sheet)) {
        const Pt q = map(point_or(*power, "pos"));
        std::string net = string_or(*power, "net");
        const std::string glyph = uppercase(string_or(*power, "symbol", net));
        const Pen pen{kPower, std::max(0.25, map.length(0.3)), {}};
        const double r = std::max(1.2, map.length(1.6));
        if (glyph == "GNDA" || glyph == "GNDD") {
            p.line(q, {q.x, q.y + r * 1.3}, pen);
            const std::vector<Pt> tri{{q.x - r, q.y + r * 1.3}, {q.x + r, q.y + r * 1.3},
                                      {q.x, q.y + r * 2.2}};
            p.polyline(tri, true, pen);
            p.text({q.x + r + 0.7, q.y + r * 1.7}, net, 1.8, 0.0, Align::Left, kPower);
        } else if (glyph == "GND" || glyph == "0" || glyph == "VSS") {
            p.line(q, {q.x, q.y + r * 1.2}, pen);
            p.line({q.x - r, q.y + r * 1.2}, {q.x + r, q.y + r * 1.2}, pen);
            p.line({q.x - r * 0.65, q.y + r * 1.65}, {q.x + r * 0.65, q.y + r * 1.65}, pen);
            p.line({q.x - r * 0.3, q.y + r * 2.05}, {q.x + r * 0.3, q.y + r * 2.05}, pen);
            p.text({q.x + r + 0.7, q.y + r * 1.2}, net, 1.8, 0.0, Align::Left, kPower);
        } else {
            p.line(q, {q.x, q.y - r * 1.35}, pen);
            const std::vector<Pt> arrow{{q.x, q.y - r * 2.15}, {q.x - r * 0.7, q.y - r * 1.25},
                                        {q.x + r * 0.7, q.y - r * 1.25}};
            p.fill(arrow, kPower);
            p.text({q.x, q.y - r * 2.8}, net, 1.8, 0.0, Align::Center, kPower);
        }
    }
}

void draw_labels(Painter& p, const json& sch, const SheetPage& sheet,
                 const std::string& first_sheet, const Mapper& map) {
    for (const json* label : items_on_sheet(sch, "labels", sheet, first_sheet)) {
        const Pt q = map(point_or(*label, "pos"));
        const double a = -number_or(*label, "angle", 0.0) * kPi / 180.0;
        const Pt d{std::cos(a), std::sin(a)};
        const Pt n{-d.y, d.x};
        const std::string text = string_or(*label, "text");
        const std::string scope = string_or(*label, "scope", "local");
        const double leader = 2.2;
        const Pt end{q.x + d.x * leader, q.y + d.y * leader};
        const Pen pen{kNet, 0.25, {}};
        p.line(q, end, pen);
        draw_filled_circle(p, q, 0.3, kNet);
        const double tw = std::max(4.0, 1.3 + text.size() * 1.05);
        const double th = 3.2;
        if (scope == "global") {
            std::vector<Pt> flag{{end.x, end.y},
                                 {end.x + d.x * 1.1 + n.x * th * 0.5,
                                  end.y + d.y * 1.1 + n.y * th * 0.5},
                                 {end.x + d.x * (tw - 1.1) + n.x * th * 0.5,
                                  end.y + d.y * (tw - 1.1) + n.y * th * 0.5},
                                 {end.x + d.x * tw, end.y + d.y * tw},
                                 {end.x + d.x * (tw - 1.1) - n.x * th * 0.5,
                                  end.y + d.y * (tw - 1.1) - n.y * th * 0.5},
                                 {end.x + d.x * 1.1 - n.x * th * 0.5,
                                  end.y + d.y * 1.1 - n.y * th * 0.5}};
            p.polyline(flag, true, pen);
            p.text({end.x + d.x * tw * 0.5, end.y + d.y * tw * 0.5}, text, 1.7,
                   -number_or(*label, "angle", 0.0), Align::Center, kNet);
        } else if (scope == "hier") {
            std::vector<Pt> tab{{end.x, end.y},
                                {end.x + d.x * 1.5 + n.x * th * 0.5,
                                 end.y + d.y * 1.5 + n.y * th * 0.5},
                                {end.x + d.x * tw + n.x * th * 0.5,
                                 end.y + d.y * tw + n.y * th * 0.5},
                                {end.x + d.x * tw - n.x * th * 0.5,
                                 end.y + d.y * tw - n.y * th * 0.5},
                                {end.x + d.x * 1.5 - n.x * th * 0.5,
                                 end.y + d.y * 1.5 - n.y * th * 0.5}};
            p.polyline(tab, true, pen);
            p.text({end.x + d.x * (tw * 0.55), end.y + d.y * (tw * 0.55)}, text, 1.7,
                   -number_or(*label, "angle", 0.0), Align::Center, kNet);
        } else {
            const Pt tick{end.x + n.x * 0.8, end.y + n.y * 0.8};
            p.line(end, tick, pen);
            p.text({end.x + d.x * 0.8 + n.x * -1.2, end.y + d.y * 0.8 + n.y * -1.2}, text,
                   1.8, -number_or(*label, "angle", 0.0), Align::Left, kNet);
        }
    }
}

std::map<std::string, std::string> sheet_names(const json& sch) {
    std::map<std::string, std::string> out;
    if (sch.is_object() && sch.contains("sheets") && sch["sheets"].is_array())
        for (const json& sh : sch["sheets"])
            if (sh.is_object()) {
                const std::string id = string_or(sh, "id");
                if (!id.empty()) out[id] = string_or(sh, "name", id);
            }
    return out;
}

void draw_sheet_symbols(Painter& p, const json& sch, const SheetPage& sheet,
                        const std::string& first_sheet, const Mapper& map) {
    const std::map<std::string, std::string> names = sheet_names(sch);
    for (const json* hs : items_on_sheet(sch, "sheet_symbols", sheet, first_sheet)) {
        const Pt center_w = point_or(*hs, "pos");
        Pt size = point_or(*hs, "size", {32.0, 20.0});
        size.x = std::max(12.0, std::fabs(size.x));
        size.y = std::max(10.0, std::fabs(size.y));
        const Pt center = map(center_w);
        const double w = map.length(size.x), h = map.length(size.y);
        const Pt tl{center.x - w * 0.5, center.y - h * 0.5};
        const Pen pen{kNet, std::max(0.25, map.length(0.3)), {}};
        p.rect(tl, w, h, pen);
        p.line({tl.x, tl.y + 4.2}, {tl.x + w, tl.y + 4.2}, pen);
        const std::string child = string_or(*hs, "child");
        auto it = names.find(child);
        p.text({center.x, tl.y + 2.2}, it == names.end() ? child : it->second, 2.2, 0.0,
               Align::Center, kNet);

        std::vector<const json*> pins;
        if (hs->contains("pins") && (*hs)["pins"].is_array())
            for (const json& pin : (*hs)["pins"])
                if (pin.is_object()) pins.push_back(&pin);
        std::stable_sort(pins.begin(), pins.end(), [](const json* a, const json* b) {
            return string_or(*a, "name") < string_or(*b, "name");
        });
        for (const json* pin : pins) {
            const Pt q = map(point_or(*pin, "pos"));
            const double dl = std::fabs(q.x - tl.x), dr = std::fabs(q.x - (tl.x + w));
            const double dt = std::fabs(q.y - tl.y), db = std::fabs(q.y - (tl.y + h));
            const double nearest = std::min({dl, dr, dt, db});
            Pt edge = q;
            Pt label = q;
            Align align = Align::Left;
            if (nearest == dl) {
                edge = {tl.x, std::clamp(q.y, tl.y, tl.y + h)};
                label = {edge.x + 1.0, edge.y - 1.0};
            } else if (nearest == dr) {
                edge = {tl.x + w, std::clamp(q.y, tl.y, tl.y + h)};
                label = {edge.x - 1.0, edge.y - 1.0};
                align = Align::Right;
            } else if (nearest == dt) {
                edge = {std::clamp(q.x, tl.x, tl.x + w), tl.y};
                label = {edge.x, edge.y + 1.5};
                align = Align::Center;
            } else {
                edge = {std::clamp(q.x, tl.x, tl.x + w), tl.y + h};
                label = {edge.x, edge.y - 1.5};
                align = Align::Center;
            }
            p.line(q, edge, pen);
            draw_filled_circle(p, q, 0.28, kNet);
            p.text(label, string_or(*pin, "name"), 1.55, 0.0, align, kNet);
        }
    }
}

void draw_texts(Painter& p, const json& sch, const SheetPage& sheet,
                const std::string& first_sheet, const Mapper& map) {
    for (const json* text : items_on_sheet(sch, "texts", sheet, first_sheet))
        p.text(map(point_or(*text, "pos")), string_or(*text, "text"),
               std::max(0.6, map.length(number_or(*text, "size", 1.27))),
               -number_or(*text, "angle", 0.0), Align::Left, kInk);
}

void render_page(Painter& p, const json& sch, const SheetPage& sheet, const PlotOpts& opts,
                 double page_w, double page_h, const std::string& first_sheet) {
    const double max_margin = std::max(1.0, std::min(page_w, page_h) * 0.5 - 2.0);
    const double margin = std::clamp(opts.margin_mm, 1.0, max_margin);
    const Bounds bounds = content_bounds(sch, sheet, first_sheet);
    const double pad = 3.0;
    const double avail_w = std::max(1.0, page_w - 2.0 * (margin + pad));
    const double avail_h = std::max(1.0, page_h - 2.0 * (margin + pad));
    double scale = 1.0;
    Pt wc{0.0, 0.0};
    if (bounds.valid()) {
        wc = {(bounds.min_x + bounds.max_x) * 0.5, (bounds.min_y + bounds.max_y) * 0.5};
        if (bounds.width() > 0.0) scale = std::min(scale, avail_w / bounds.width());
        if (bounds.height() > 0.0) scale = std::min(scale, avail_h / bounds.height());
    }
    scale = std::max(scale, 0.0001);
    const Mapper map{scale, wc, {page_w * 0.5, page_h * 0.5}};

    draw_frame(p, page_w, page_h, margin, opts.frame);
    draw_title_block(p, sch, sheet, page_w, page_h, margin, scale, opts.title_block);
    for (const json* graphic : items_on_sheet(sch, "graphics", sheet, first_sheet))
        draw_graphic(p, *graphic, map, kInk);
    draw_bus_and_wires(p, sch, sheet, first_sheet, map);
    draw_junctions_and_nc(p, sch, sheet, first_sheet, map);
    draw_symbols(p, sch, sheet, first_sheet, map, opts);
    draw_power(p, sch, sheet, first_sheet, map);
    draw_labels(p, sch, sheet, first_sheet, map);
    draw_sheet_symbols(p, sch, sheet, first_sheet, map);
    draw_texts(p, sch, sheet, first_sheet, map);
}

std::string make_pdf(const json& sch, const PlotOpts& opts,
                     const std::vector<SheetPage>& pages, double width_mm, double height_mm,
                     const std::string& first_sheet) {
    std::vector<std::string> streams;
    streams.reserve(pages.size());
    for (const SheetPage& page : pages) {
        PdfPainter painter(height_mm);
        render_page(painter, sch, page, opts, width_mm, height_mm, first_sheet);
        streams.push_back(painter.finish());
    }

    const int object_count = 3 + static_cast<int>(pages.size()) * 2;
    std::vector<std::string> objects(static_cast<std::size_t>(object_count + 1));
    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>";
    std::ostringstream kids;
    kids << '[';
    for (std::size_t i = 0; i < pages.size(); ++i) kids << ' ' << (4 + static_cast<int>(i) * 2) << " 0 R";
    kids << " ]";
    objects[2] = "<< /Type /Pages /Count " + std::to_string(pages.size()) + " /Kids " + kids.str() + " >>";
    objects[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>";
    for (std::size_t i = 0; i < pages.size(); ++i) {
        const int page_obj = 4 + static_cast<int>(i) * 2;
        const int content_obj = page_obj + 1;
        objects[static_cast<std::size_t>(page_obj)] =
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + fmt(width_mm * kPtPerMm) + " " +
            fmt(height_mm * kPtPerMm) +
            "] /Resources << /Font << /F1 3 0 R >> >> /Contents " + std::to_string(content_obj) + " 0 R >>";
        objects[static_cast<std::size_t>(content_obj)] =
            "<< /Length " + std::to_string(streams[i].size()) + " >>\nstream\n" + streams[i] + "endstream";
    }

    std::string pdf = "%PDF-1.4\n%\xE2\xE3\xCF\xD3\n";
    std::vector<std::size_t> offsets(static_cast<std::size_t>(object_count + 1), 0);
    for (int i = 1; i <= object_count; ++i) {
        offsets[static_cast<std::size_t>(i)] = pdf.size();
        pdf += std::to_string(i) + " 0 obj\n" + objects[static_cast<std::size_t>(i)] + "\nendobj\n";
    }
    const std::size_t xref = pdf.size();
    std::ostringstream tail;
    tail.imbue(std::locale::classic());
    tail << "xref\n0 " << object_count + 1 << "\n0000000000 65535 f \n";
    for (int i = 1; i <= object_count; ++i)
        tail << std::setw(10) << std::setfill('0') << offsets[static_cast<std::size_t>(i)]
             << " 00000 n \n";
    tail << "trailer\n<< /Size " << object_count + 1 << " /Root 1 0 R >>\nstartxref\n" << xref
         << "\n%%EOF\n";
    pdf += tail.str();
    return pdf;
}

}  // namespace

std::pair<double, double> paper_size_mm(const std::string& paper) {
    std::string name = uppercase(paper);
    if (name == "A0") return {1189.0, 841.0};
    if (name == "A1") return {841.0, 594.0};
    if (name == "A2") return {594.0, 420.0};
    if (name == "A3") return {420.0, 297.0};
    if (name == "LETTER") return {279.4, 215.9};
    if (name == "TABLOID") return {431.8, 279.4};
    return {297.0, 210.0};
}

std::vector<std::pair<std::string, std::string>> schematic_svg_pages(const json& sch,
                                                                      const PlotOpts& o) {
    require_svg_sheet(o.sheet, true);
    const std::vector<SheetPage> all = selected_sheets(sch, PlotOpts{});
    for (const SheetPage& page : all) require_svg_sheet(page.id);
    const auto [width, height] = paper_size_mm(o.paper);
    const std::vector<SheetPage> pages = selected_sheets(sch, o);
    const std::string first_sheet = all.empty() ? "sh1" : all.front().id;
    std::vector<std::pair<std::string, std::string>> out;
    out.reserve(pages.size());
    for (const SheetPage& page : pages) {
        SvgPainter painter(width, height);
        render_page(painter, sch, page, o, width, height, first_sheet);
        out.push_back({page.id, painter.finish()});
    }
    return out;
}

std::string schematic_svg(const json& sch, const PlotOpts& o) {
    std::vector<std::pair<std::string, std::string>> pages = schematic_svg_pages(sch, o);
    return pages.empty() ? std::string() : pages.front().second;
}

std::string schematic_pdf(const json& sch, const PlotOpts& o) {
    const auto [width, height] = paper_size_mm(o.paper);
    const std::vector<SheetPage> pages = selected_sheets(sch, o);
    const std::vector<SheetPage> all = selected_sheets(sch, PlotOpts{});
    const std::string first_sheet = all.empty() ? "sh1" : all.front().id;
    return make_pdf(sch, o, pages, width, height, first_sheet);
}

}  // namespace chudware::ecad::plot
