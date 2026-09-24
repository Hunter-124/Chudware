#include "ecad/fabformats.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <map>
#include <regex>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "ecad/schema.h"

namespace chudware::ecad::fab {
namespace {

using nlohmann::json;

constexpr double kPi = 3.14159265358979323846;

// Python round(): round-half-to-even (banker's). std::round is half-away.
double py_round(double v) {
    double fl = std::floor(v);
    double diff = v - fl;
    if (diff < 0.5) return fl;
    if (diff > 0.5) return fl + 1.0;
    double ev = std::fmod(fl, 2.0);
    return (ev == 0.0) ? fl : fl + 1.0;
}

double getf(const json& v, double dflt = 0.0) {
    return v.is_number() ? v.get<double>() : dflt;
}
std::string gets(const json& v, const std::string& dflt = "") {
    return v.is_string() ? v.get<std::string>() : dflt;
}

// printf-style "%.Nf" — matches Python f"{x:.Nf}" digit-for-digit.
std::string fmt_f(double v, int prec) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.*f", prec, v);
    return std::string(buf);
}

// Gerber coordinate: 4.6 integer (1mm -> 1e6 units). str(round(mm*1e6)).
constexpr double kGerberScale = 1000000.0;
std::string gfmt(double mm) {
    long long n = static_cast<long long>(py_round(mm * kGerberScale));
    return std::to_string(n);
}

// Excellon coordinate: 3.3 (1mm -> 1000 units). str(round(mm*1000)).
constexpr double kExScale = 1000.0;
std::string exfmt(double mm) {
    long long n = static_cast<long long>(py_round(mm * kExScale));
    return std::to_string(n);
}

// local_to_world (mirror, rotate CCW by angle deg, translate by pos)
std::pair<double, double> rot(double x, double y, double deg) {
    double r = deg * kPi / 180.0;
    double c = std::cos(r), s = std::sin(r);
    return {x * c - y * s, x * s + y * c};
}
std::pair<double, double> apply_mirror(double x, double y, const std::string& mirror) {
    if (mirror == "x") return {-x, y};
    if (mirror == "y") return {x, -y};
    return {x, y};
}
std::pair<double, double> local_to_world(double x, double y, const json& pos, double angle,
                                         const std::string& mirror) {
    auto [mx, my] = apply_mirror(x, y, mirror);
    auto [rx, ry] = rot(mx, my, angle);
    return {rx + getf(pos[0]), ry + getf(pos[1])};
}

// ── natural sort key: re.split(r"(\d+)", ref) -> int|lower parts ──────────────
struct NatPart {
    bool is_num;
    long long num;     // valid when is_num
    std::string text;  // lower-cased, valid when !is_num
};
std::vector<NatPart> natural_key(const std::string& ref) {
    // Python re.split with a capturing group keeps the delimiters and also emits
    // empty strings at boundaries; we replicate that exactly so comparisons match.
    static const std::regex re("([0-9]+)");
    std::vector<NatPart> out;
    std::sregex_token_iterator it(ref.begin(), ref.end(), re, {-1, 1}), end;
    for (; it != end; ++it) {
        std::string p = *it;
        bool digit = !p.empty() && std::all_of(p.begin(), p.end(),
                                               [](unsigned char c) { return std::isdigit(c); });
        if (digit) {
            out.push_back({true, std::stoll(p), ""});
        } else {
            std::string low = p;
            std::transform(low.begin(), low.end(), low.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            out.push_back({false, 0, low});
        }
    }
    return out;
}
// Compare two natural keys (mirrors Python tuple comparison; int<str type rules
// never collide here because both engines split identically).
bool nat_less(const std::vector<NatPart>& a, const std::vector<NatPart>& b) {
    size_t n = std::min(a.size(), b.size());
    for (size_t i = 0; i < n; ++i) {
        const NatPart& x = a[i];
        const NatPart& y = b[i];
        if (x.is_num != y.is_num) {
            // Python would raise comparing int vs str; in practice the split always
            // yields the same alternation for ref-like strings, so treat num<str.
            return x.is_num;
        }
        if (x.is_num) {
            if (x.num != y.num) return x.num < y.num;
        } else {
            if (x.text != y.text) return x.text < y.text;
        }
    }
    return a.size() < b.size();
}

// ─────────────────────────────────────────────────────── Gerber layer extension
const std::map<std::string, std::string> kLayerExt = {
    {"F.Cu", ".GTL"},   {"B.Cu", ".GBL"},    {"F.SilkS", ".GTO"}, {"B.SilkS", ".GBO"},
    {"F.Mask", ".GTS"}, {"B.Mask", ".GBS"},  {"F.Paste", ".GTP"}, {"B.Paste", ".GBP"},
    {"Edge.Cuts", ".GM1"}, {"F.Fab", ".GM2"}, {"B.Fab", ".GM3"},  {"F.CrtYd", ".GM4"},
    {"B.CrtYd", ".GM5"},
};

bool is_copper(const std::string& layer_id) {
    static const std::regex re("^(F|B|In[0-9]+)\\.Cu$");
    return std::regex_match(layer_id, re);
}

// ─────────────────────────────────────────────────────────── aperture table
// Aperture key: (shape, w, h, rr) with w/h/rr rounded to 6 dp (matches the
// Python tuple keys). D-codes are assigned in first-seen order starting at D10.
struct ApKey {
    std::string shape;
    double w, h, rr;
    bool operator<(const ApKey& o) const {
        return std::tie(shape, w, h, rr) < std::tie(o.shape, o.w, o.h, o.rr);
    }
    bool operator==(const ApKey& o) const {
        return shape == o.shape && w == o.w && h == o.h && rr == o.rr;
    }
};

double round6(double v) { return py_round(v * 1e6) / 1e6; }

struct ApertureTable {
    std::vector<std::pair<ApKey, int>> table;  // preserve insertion order
    int next_d = 10;

    int get_or_add(const ApKey& key) {
        for (auto& kv : table)
            if (kv.first == key) return kv.second;
        int d = next_d++;
        table.push_back({key, d});
        return d;
    }
    int get_or_add_pad(const json& pad) {
        std::string shape = gets(pad.value("shape", json("rect")), "rect");
        double w = getf(pad["size"][0]), h = getf(pad["size"][1]);
        double rr = pad.contains("roundrect_ratio") ? getf(pad["roundrect_ratio"]) : 0.0;
        return get_or_add(ApKey{shape, round6(w), round6(h), round6(rr)});
    }
    int get_or_add_track(double width) {
        return get_or_add(ApKey{"circle", round6(width), round6(width), 0.0});
    }
    int get_or_add_via(double diameter) {
        return get_or_add(ApKey{"circle", round6(diameter), round6(diameter), 0.0});
    }

    // roundrect polygon vertices centered at origin (matches gerber._roundrect_polygon)
    static std::vector<std::pair<double, double>> roundrect_polygon(double w, double h, double r,
                                                                    int n_arc) {
        std::vector<std::pair<double, double>> pts;
        double hw = w / 2, hh = h / 2;
        r = std::min({r, hw, hh});
        struct C { double cx, cy, start; };
        std::vector<C> corners = {
            {hw - r, hh - r, 0},     {-hw + r, hh - r, 90},
            {-hw + r, -hh + r, 180}, {hw - r, -hh + r, 270},
        };
        for (const C& c : corners) {
            for (int i = 0; i <= n_arc; ++i) {
                double a = (c.start + i * 90.0 / n_arc) * kPi / 180.0;
                pts.push_back({c.cx + r * std::cos(a), c.cy + r * std::sin(a)});
            }
        }
        return pts;
    }

    // (macro_lines, ADD_line) for a key (mirrors _aperture_def_line)
    std::pair<std::vector<std::string>, std::string> def_line(const ApKey& key, int d) const {
        std::vector<std::string> macros;
        std::string add;
        const std::string& shape = key.shape;
        double w = key.w, h = key.h, rr = key.rr;
        if (shape == "circle") {
            add = "%ADD" + std::to_string(d) + "C," + fmt_f(w, 6) + "*%";
        } else if (shape == "oval") {
            add = "%ADD" + std::to_string(d) + "O," + fmt_f(w, 6) + "X" + fmt_f(h, 6) + "*%";
        } else if (shape == "rect" || shape == "trapezoid") {
            add = "%ADD" + std::to_string(d) + "R," + fmt_f(w, 6) + "X" + fmt_f(h, 6) + "*%";
        } else if (shape == "roundrect") {
            std::string name = "RNDRCT" + std::to_string(d);
            double corner_r = std::min(w, h) * rr;
            auto pts = roundrect_polygon(w, h, corner_r, 8);
            std::string pts_str;
            for (size_t i = 0; i < pts.size(); ++i) {
                if (i) pts_str += "X";
                pts_str += fmt_f(pts[i].first, 6) + "X" + fmt_f(pts[i].second, 6);
            }
            std::string macro_body = "4,1," + std::to_string(pts.size()) + "," + pts_str + "X" +
                                     fmt_f(pts[0].first, 6) + "X" + fmt_f(pts[0].second, 6) + "X0*";
            macros.push_back("%AM" + name + "*\n" + macro_body + "\n%");
            add = "%ADD" + std::to_string(d) + name + ",*%";
        } else {
            add = "%ADD" + std::to_string(d) + "R," + fmt_f(w, 6) + "X" + fmt_f(h, 6) + "*%";
        }
        return {macros, add};
    }

    std::vector<std::string> header_lines() const {
        std::vector<std::pair<ApKey, int>> sorted_items = table;
        std::sort(sorted_items.begin(), sorted_items.end(),
                  [](const auto& a, const auto& b) { return a.second < b.second; });
        std::vector<std::string> all_macros, add_lines;
        for (const auto& kv : sorted_items) {
            auto [macros, add] = def_line(kv.first, kv.second);
            for (auto& m : macros) all_macros.push_back(m);
            add_lines.push_back(add);
        }
        all_macros.insert(all_macros.end(), add_lines.begin(), add_lines.end());
        return all_macros;
    }
};

// ── op records ───────────────────────────────────────────────────────────────
struct Op {
    int kind;  // 0=stroke 1=flash 2=region
    int d = 0;
    double x1 = 0, y1 = 0, x2 = 0, y2 = 0;                  // stroke / flash (x1,y1)
    std::vector<std::pair<double, double>> pts;            // region
};

bool layer_in(const json& obj, const char* key, const std::string& layer) {
    if (!obj.contains(key) || !obj[key].is_array()) return false;
    for (const auto& l : obj[key])
        if (l.is_string() && l.get<std::string>() == layer) return true;
    return false;
}

}  // namespace

// ────────────────────────────────────────────────────── write_gerber_layer
std::string write_gerber_layer(const json& board, const std::string& layer_id) {
    ApertureTable ap;
    std::vector<Op> ops;

    json origin = board.value("origin", json::array({0.0, 0.0}));
    double ox = getf(origin[0]), oy = getf(origin[1]);
    auto bxy = [&](double x, double y) { return std::make_pair(x - ox, y - oy); };

    // tracks
    if (board.contains("tracks"))
        for (const auto& track : board["tracks"]) {
            if (gets(track.value("layer", json("")), "") != layer_id) continue;
            std::string kind = gets(track.value("kind", json("segment")), "segment");
            if (kind == "segment") {
                int d = ap.get_or_add_track(getf(track["width"]));
                auto [x1, y1] = bxy(getf(track["p1"][0]), getf(track["p1"][1]));
                auto [x2, y2] = bxy(getf(track["p2"][0]), getf(track["p2"][1]));
                ops.push_back({0, d, x1, y1, x2, y2, {}});
            } else if (kind == "arc") {
                // Arc track: the oracle (engine/ecad/fab/gerber.py) does NOT emit
                // G02/G03 circular interpolation — it tessellates the arc into short
                // chord strokes (one stroke per ~5° step) at the track aperture, so the
                // port mirrors that chord-stroke approximation exactly.
                int d = ap.get_or_add_track(getf(track["width"]));
                double cx = getf(track["center"][0]), cy = getf(track["center"][1]);
                double a0 = (track.contains("a0") ? getf(track["a0"]) : 0.0) * kPi / 180.0;
                double a1 = (track.contains("a1") ? getf(track["a1"]) : 90.0) * kPi / 180.0;
                double da = a1 - a0;
                // n = max(4, int(abs(da) / radians(5)))  — int() truncates toward zero
                int n = std::max(4, static_cast<int>(std::abs(da) / (5.0 * kPi / 180.0)));
                // radius from p1 to center (constant; oracle recomputes it per point)
                double r = std::sqrt(std::pow(getf(track["p1"][0]) - cx, 2) +
                                     std::pow(getf(track["p1"][1]) - cy, 2));
                std::vector<std::pair<double, double>> pts;
                for (int i = 0; i <= n; ++i) {
                    double a = a0 + da * i / n;
                    pts.push_back(bxy(cx + r * std::cos(a), cy + r * std::sin(a)));
                }
                for (size_t i = 0; i + 1 < pts.size(); ++i)
                    ops.push_back({0, d, pts[i].first, pts[i].second, pts[i + 1].first,
                                   pts[i + 1].second, {}});
            }
        }

    // vias
    if (is_copper(layer_id) && board.contains("vias"))
        for (const auto& via : board["vias"]) {
            if (!layer_in(via, "layers", layer_id)) continue;
            int d = ap.get_or_add_via(getf(via["diameter"]));
            auto [x, y] = bxy(getf(via["at"][0]), getf(via["at"][1]));
            ops.push_back({1, d, x, y, 0, 0, {}});
        }

    // pads
    if (board.contains("footprints"))
        for (const auto& fp : board["footprints"])
            if (fp.contains("pads"))
                for (const auto& pad : fp["pads"]) {
                    if (!layer_in(pad, "layers", layer_id)) continue;
                    int d = ap.get_or_add_pad(pad);
                    auto [wx, wy] = schema::pad_world(fp, pad);
                    auto [bx, by] = bxy(wx, wy);
                    ops.push_back({1, d, bx, by, 0, 0, {}});
                }

    // zone fills as G36/G37 regions
    if (board.contains("zones"))
        for (const auto& zone : board["zones"]) {
            if (gets(zone.value("layer", json("")), "") != layer_id) continue;
            if (!zone.contains("filled_polys")) continue;
            for (const auto& poly : zone["filled_polys"]) {
                if (!poly.is_array() || poly.size() < 3) continue;
                std::vector<std::pair<double, double>> pts;
                for (const auto& p : poly) pts.push_back(bxy(getf(p[0]), getf(p[1])));
                ops.push_back({2, 0, 0, 0, 0, 0, pts});
            }
        }

    // board-level graphics
    if (board.contains("graphics"))
        for (const auto& gfx : board["graphics"]) {
            if (gets(gfx.value("layer", json("")), "") != layer_id) continue;
            std::string gt = gets(gfx.value("type", json("line")), "line");
            if (gt == "line") {
                double width = gfx.contains("width") ? getf(gfx["width"], 0.1) : 0.1;
                int d = ap.get_or_add_track(width);
                json p1 = gfx.value("p1", json::array({0, 0}));
                json p2 = gfx.value("p2", json::array({0, 0}));
                auto [x1, y1] = bxy(getf(p1[0]), getf(p1[1]));
                auto [x2, y2] = bxy(getf(p2[0]), getf(p2[1]));
                ops.push_back({0, d, x1, y1, x2, y2, {}});
            } else if (gt == "circle") {
                double width = gfx.contains("width") ? getf(gfx["width"], 0.1) : 0.1;
                int d = ap.get_or_add_track(width);
                double cx = getf(gfx["center"][0]), cy = getf(gfx["center"][1]);
                double r = getf(gfx["radius"]);
                int n = 36;
                std::vector<std::pair<double, double>> pts;
                for (int i = 0; i <= n; ++i)
                    pts.push_back({cx + r * std::cos(2 * kPi * i / n),
                                   cy + r * std::sin(2 * kPi * i / n)});
                for (size_t i = 0; i + 1 < pts.size(); ++i) {
                    auto [x1, y1] = bxy(pts[i].first, pts[i].second);
                    auto [x2, y2] = bxy(pts[i + 1].first, pts[i + 1].second);
                    ops.push_back({0, d, x1, y1, x2, y2, {}});
                }
            }
        }

    // Edge.Cuts outline
    if (layer_id == "Edge.Cuts" && board.contains("outline") &&
        board["outline"].contains("shapes"))
        for (const auto& shape : board["outline"]["shapes"]) {
            double width = shape.contains("width") ? getf(shape["width"], 0.05) : 0.05;
            int d = ap.get_or_add_track(width);
            std::string st = gets(shape.value("type", json("line")), "line");
            if (st == "line") {
                json p1 = shape.value("p1", json::array({0, 0}));
                json p2 = shape.value("p2", json::array({0, 0}));
                auto [x1, y1] = bxy(getf(p1[0]), getf(p1[1]));
                auto [x2, y2] = bxy(getf(p2[0]), getf(p2[1]));
                ops.push_back({0, d, x1, y1, x2, y2, {}});
            } else if (st == "circle") {
                double cx = getf(shape["center"][0]), cy = getf(shape["center"][1]);
                double r = getf(shape["radius"]);
                int n = 36;
                std::vector<std::pair<double, double>> pts;
                for (int i = 0; i <= n; ++i)
                    pts.push_back({cx + r * std::cos(2 * kPi * i / n),
                                   cy + r * std::sin(2 * kPi * i / n)});
                for (size_t i = 0; i + 1 < pts.size(); ++i) {
                    auto [x1, y1] = bxy(pts[i].first, pts[i].second);
                    auto [x2, y2] = bxy(pts[i + 1].first, pts[i + 1].second);
                    ops.push_back({0, d, x1, y1, x2, y2, {}});
                }
            }
            // arc outline: oracle uses center/radius/a0/a1 fields not in the schema
            // arc model; no parity fixture carries them. (Deferred — see report.)
        }

    // footprint graphics
    if (board.contains("footprints"))
        for (const auto& fp : board["footprints"]) {
            json fp_pos = fp.value("pos", json::array({0.0, 0.0}));
            double fp_angle = fp.contains("angle") ? getf(fp["angle"]) : 0.0;
            std::string fp_side = gets(fp.value("side", json("top")), "top");
            std::string mirror = (fp_side == "bottom") ? "x" : "none";
            if (!fp.contains("graphics")) continue;
            for (const auto& gfx : fp["graphics"]) {
                if (gets(gfx.value("layer", json("")), "") != layer_id) continue;
                std::string gt = gets(gfx.value("type", json("line")), "line");
                double width = gfx.contains("width") ? getf(gfx["width"], 0.1) : 0.1;
                int d = ap.get_or_add_track(width);
                if (gt == "line") {
                    json p1 = gfx.value("p1", json::array({0, 0}));
                    json p2 = gfx.value("p2", json::array({0, 0}));
                    auto w1 = local_to_world(getf(p1[0]), getf(p1[1]), fp_pos, fp_angle, mirror);
                    auto w2 = local_to_world(getf(p2[0]), getf(p2[1]), fp_pos, fp_angle, mirror);
                    auto [x1, y1] = bxy(w1.first, w1.second);
                    auto [x2, y2] = bxy(w2.first, w2.second);
                    ops.push_back({0, d, x1, y1, x2, y2, {}});
                } else if (gt == "circle") {
                    double cx0 = getf(gfx["center"][0]), cy0 = getf(gfx["center"][1]);
                    double r = gfx.contains("radius") ? getf(gfx["radius"], 1.0) : 1.0;
                    int n = 36;
                    std::vector<std::pair<double, double>> wpts;
                    for (int i = 0; i <= n; ++i) {
                        double rx = cx0 + r * std::cos(2 * kPi * i / n);
                        double ry = cy0 + r * std::sin(2 * kPi * i / n);
                        auto w = local_to_world(rx, ry, fp_pos, fp_angle, mirror);
                        wpts.push_back(bxy(w.first, w.second));
                    }
                    for (size_t i = 0; i + 1 < wpts.size(); ++i)
                        ops.push_back({0, d, wpts[i].first, wpts[i].second, wpts[i + 1].first,
                                       wpts[i + 1].second, {}});
                }
            }
        }

    // ── emit ──
    std::vector<std::string> lines;
    lines.push_back("%FSLAX46Y46*%");
    lines.push_back("%MOMM*%");
    for (auto& h : ap.header_lines()) lines.push_back(h);

    int current_d = -1;
    for (const Op& op : ops) {
        if (op.kind == 0) {  // stroke
            if (op.d != current_d) {
                lines.push_back("D" + std::to_string(op.d) + "*");
                current_d = op.d;
            }
            lines.push_back("X" + gfmt(op.x1) + "Y" + gfmt(op.y1) + "D02*");
            lines.push_back("X" + gfmt(op.x2) + "Y" + gfmt(op.y2) + "D01*");
        } else if (op.kind == 1) {  // flash
            if (op.d != current_d) {
                lines.push_back("D" + std::to_string(op.d) + "*");
                current_d = op.d;
            }
            lines.push_back("X" + gfmt(op.x1) + "Y" + gfmt(op.y1) + "D03*");
        } else {  // region
            lines.push_back("G36*");
            lines.push_back("X" + gfmt(op.pts[0].first) + "Y" + gfmt(op.pts[0].second) + "D02*");
            for (size_t i = 1; i < op.pts.size(); ++i)
                lines.push_back("X" + gfmt(op.pts[i].first) + "Y" + gfmt(op.pts[i].second) + "D01*");
            lines.push_back("X" + gfmt(op.pts[0].first) + "Y" + gfmt(op.pts[0].second) + "D01*");
            lines.push_back("G37*");
        }
    }
    lines.push_back("M02*");

    std::string out;
    for (size_t i = 0; i < lines.size(); ++i) {
        if (i) out += "\n";
        out += lines[i];
    }
    out += "\n";
    return out;
}

// ────────────────────────────────────────────────────────── write_gerbers
std::map<std::string, std::string> write_gerbers(const json& board) {
    int n_cu = board.contains("stackup") && board["stackup"].is_object()
                   ? board["stackup"].value("copper_layers", 2) : 2;
    std::vector<std::string> candidate = schema::copper_layer_ids(n_cu);
    for (const char* l : {"F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "F.Paste", "B.Paste", "F.Fab",
                          "B.Fab", "Edge.Cuts"})
        candidate.push_back(l);
    std::map<std::string, std::string> result;
    for (const std::string& layer_id : candidate)
        result[layer_id] = write_gerber_layer(board, layer_id);
    return result;
}

// ────────────────────────────────────────────────────────── write_drill
namespace {
struct DrillHit {
    double diameter, x, y;
    bool oval;
    double ex, ey;  // slot end (oval)
};

void collect_drills(const json& board, std::vector<DrillHit>& pth, std::vector<DrillHit>& npth) {
    json origin = board.value("origin", json::array({0.0, 0.0}));
    double ox = getf(origin[0]), oy = getf(origin[1]);

    if (board.contains("footprints"))
        for (const auto& fp : board["footprints"])
            if (fp.contains("pads"))
                for (const auto& pad : fp["pads"]) {
                    std::string ptype = gets(pad.value("type", json("smd")), "smd");
                    if (ptype != "tht" && ptype != "np_tht") continue;
                    if (!pad.contains("drill") || pad["drill"].is_null()) continue;
                    const json& drill = pad["drill"];

                    auto [wx0, wy0] = schema::pad_world(fp, pad);
                    double wx = wx0 - ox, wy = wy0 - oy;
                    double dia = drill.contains("diameter") ? getf(drill["diameter"]) : 0.0;
                    std::string dshape = gets(drill.value("shape", json("round")), "round");

                    DrillHit hit{};
                    if (dshape == "oval") {
                        double pad_w = getf(pad["size"][0]), pad_h = getf(pad["size"][1]);
                        double slot_len = std::max(pad_w, pad_h) - std::min(pad_w, pad_h);
                        double fp_angle = fp.contains("angle") ? getf(fp["angle"]) : 0.0;
                        double dx, dy;
                        if (pad_w >= pad_h) {
                            dx = slot_len / 2 * std::cos(fp_angle * kPi / 180.0);
                            dy = slot_len / 2 * std::sin(fp_angle * kPi / 180.0);
                        } else {
                            dx = slot_len / 2 * std::cos((fp_angle + 90) * kPi / 180.0);
                            dy = slot_len / 2 * std::sin((fp_angle + 90) * kPi / 180.0);
                        }
                        hit = {dia, wx - dx, wy - dy, true, wx + dx, wy + dy};
                    } else {
                        hit = {dia, wx, wy, false, 0, 0};
                    }
                    if (ptype == "np_tht")
                        npth.push_back(hit);
                    else
                        pth.push_back(hit);
                }

    if (board.contains("vias"))
        for (const auto& via : board["vias"]) {
            double vx = getf(via["at"][0]) - ox, vy = getf(via["at"][1]) - oy;
            pth.push_back({getf(via["drill"]), vx, vy, false, 0, 0});
        }
}

std::string build_excellon(const std::vector<DrillHit>& drills, bool plated) {
    std::vector<std::string> lines;
    if (drills.empty()) {
        lines = {"M48", "METRIC,TZ", "M95", "M30"};
        std::string out;
        for (size_t i = 0; i < lines.size(); ++i) {
            if (i) out += "\n";
            out += lines[i];
        }
        return out + "\n";
    }

    // tool table: distinct diameters (rounded 6dp), sorted ascending
    std::vector<double> diameters;
    {
        std::set<double> seen;
        for (const auto& d : drills) seen.insert(round6(d.diameter));
        for (double v : seen) diameters.push_back(v);  // std::set is sorted
    }
    std::map<double, int> tool_map;
    for (size_t i = 0; i < diameters.size(); ++i) tool_map[diameters[i]] = static_cast<int>(i) + 1;

    lines.push_back("M48");
    lines.push_back("METRIC,TZ");
    lines.push_back(plated ? "; Plated Through-Hole Drill File" : "; Non-Plated Drill File");

    for (double dia : diameters) {
        char tnum[8];
        std::snprintf(tnum, sizeof(tnum), "%02d", tool_map[dia]);
        lines.push_back("T" + std::string(tnum) + "C" + fmt_f(dia, 3));
    }
    lines.push_back("M95");

    // group hits by tool, preserving per-tool encounter order
    std::map<int, std::vector<const DrillHit*>> by_tool;
    for (const auto& d : drills) by_tool[tool_map[round6(d.diameter)]].push_back(&d);

    for (auto& kv : by_tool) {  // std::map sorts by tool number ascending
        char tnum[8];
        std::snprintf(tnum, sizeof(tnum), "%02d", kv.first);
        lines.push_back("T" + std::string(tnum));
        for (const DrillHit* hit : kv.second) {
            if (!hit->oval) {
                lines.push_back("X" + exfmt(hit->x) + "Y" + exfmt(hit->y));
            } else {
                lines.push_back("G85X" + exfmt(hit->x) + "Y" + exfmt(hit->y) + "G85X" +
                                exfmt(hit->ex) + "Y" + exfmt(hit->ey));
            }
        }
    }
    lines.push_back("M30");

    std::string out;
    for (size_t i = 0; i < lines.size(); ++i) {
        if (i) out += "\n";
        out += lines[i];
    }
    return out + "\n";
}
}  // namespace

std::map<std::string, std::string> write_drill(const json& board) {
    std::vector<DrillHit> pth, npth;
    collect_drills(board, pth, npth);
    return {{"pth", build_excellon(pth, true)}, {"npth", build_excellon(npth, false)}};
}

// ────────────────────────────────────────────────────────────── write_bom
namespace {
// QUOTE_MINIMAL CSV field (quote if contains delimiter, quote, CR or LF).
std::string csv_field(const std::string& s) {
    bool need = s.find(',') != std::string::npos || s.find('"') != std::string::npos ||
                s.find('\n') != std::string::npos || s.find('\r') != std::string::npos;
    if (!need) return s;
    std::string out = "\"";
    for (char c : s) {
        if (c == '"') out += "\"\"";
        else out += c;
    }
    out += "\"";
    return out;
}
}  // namespace

std::string write_bom(const json& board, const std::string& fmt, bool exclude_dnp) {
    static const std::set<std::string> kStdLower = {
        "ref", "value", "footprint", "mpn", "dnp", "attr", "model_3d", "id", "fpid",
        "linked_symbol", "pads", "graphics", "pos", "angle", "side"};

    // group key: (value, fpid, sorted user-field items). Preserve first-seen order.
    struct Group {
        std::string value, footprint, mpn;
        std::vector<std::pair<std::string, std::string>> fields;  // sorted by key
        std::vector<std::string> refs;                            // in encounter order
    };
    std::vector<Group> groups;
    auto find_group = [&](const std::string& val, const std::string& fpid,
                          const std::vector<std::pair<std::string, std::string>>& flds) -> Group* {
        for (auto& g : groups)
            if (g.value == val && g.footprint == fpid && g.fields == flds) return &g;
        return nullptr;
    };

    if (board.contains("footprints"))
        for (const auto& fp : board["footprints"]) {
            std::string ref = gets(fp.value("ref", json("?")), "?");
            std::string value = gets(fp.value("value", json("")), "");
            std::string fpid = gets(fp.value("fpid", json("")), "");
            std::string attr = gets(fp.value("attr", json("smd")), "smd");

            // user fields: fp["fields"] keys whose lower-case not in standard set
            std::map<std::string, std::string> fields;  // (also collects MPN, popped below)
            if (fp.contains("fields") && fp["fields"].is_object())
                for (auto it = fp["fields"].begin(); it != fp["fields"].end(); ++it) {
                    std::string k = it.key();
                    std::string klow = k;
                    std::transform(klow.begin(), klow.end(), klow.begin(),
                                   [](unsigned char c) { return std::tolower(c); });
                    if (kStdLower.count(klow)) continue;
                    fields[k] = it.value().is_string() ? it.value().get<std::string>()
                                                       : it.value().dump();
                }

            bool dnp = (fp.contains("dnp") && fp["dnp"].is_boolean() && fp["dnp"].get<bool>()) ||
                       attr == "virtual";
            if (exclude_dnp && dnp) continue;

            // mpn = fields.pop("MPN", fields.pop("mpn", ""))
            std::string mpn;
            if (fields.count("MPN")) { mpn = fields["MPN"]; fields.erase("MPN"); }
            else if (fields.count("mpn")) { mpn = fields["mpn"]; fields.erase("mpn"); }

            // sorted field items (std::map already sorts ascending by key)
            std::vector<std::pair<std::string, std::string>> flds(fields.begin(), fields.end());

            Group* g = find_group(value, fpid, flds);
            if (!g) {
                groups.push_back(Group{value, fpid, mpn, flds, {}});
                g = &groups.back();
            }
            g->refs.push_back(ref);
        }

    // build rows
    struct Row {
        std::string refs;  // joined sorted refs
        int qty;
        std::string value, footprint, mpn;
        std::vector<std::pair<std::string, std::string>> fields;
    };
    std::vector<Row> rows;
    for (auto& g : groups) {
        std::vector<std::string> sr = g.refs;
        std::stable_sort(sr.begin(), sr.end(),
                         [](const std::string& a, const std::string& b) {
                             return nat_less(natural_key(a), natural_key(b));
                         });
        std::string joined;
        for (size_t i = 0; i < sr.size(); ++i) {
            if (i) joined += ",";
            joined += sr[i];
        }
        rows.push_back(Row{joined, static_cast<int>(g.refs.size()), g.value, g.footprint, g.mpn,
                           g.fields});
    }
    // sort rows by natural key of first ref (stable)
    std::stable_sort(rows.begin(), rows.end(), [](const Row& a, const Row& b) {
        return nat_less(natural_key(a.refs.substr(0, a.refs.find(','))),
                        natural_key(b.refs.substr(0, b.refs.find(','))));
    });

    if (rows.empty()) {
        if (fmt == "json") return "[]";
        return "Refs,Qty,Value,Footprint,MPN\n";
    }

    // columns: standard + sorted(extra cols collected in encounter order)
    std::vector<std::string> std_cols = {"Refs", "Qty", "Value", "Footprint", "MPN"};
    std::vector<std::string> extra_cols;
    std::set<std::string> seen_extra;
    for (const Row& r : rows)
        for (const auto& kv : r.fields)
            if (!seen_extra.count(kv.first) &&
                std::find(std_cols.begin(), std_cols.end(), kv.first) == std_cols.end()) {
                extra_cols.push_back(kv.first);
                seen_extra.insert(kv.first);
            }
    std::sort(extra_cols.begin(), extra_cols.end());
    std::vector<std::string> all_cols = std_cols;
    all_cols.insert(all_cols.end(), extra_cols.begin(), extra_cols.end());

    auto field_value = [](const Row& r, const std::string& col) -> std::string {
        for (const auto& kv : r.fields)
            if (kv.first == col) return kv.second;
        return "";
    };

    if (fmt == "json") {
        // ordered_json preserves key insertion order (matches Python json.dumps of a
        // dict, which is insertion-ordered: Refs, Qty, Value, Footprint, MPN, ...).
        nlohmann::ordered_json arr = nlohmann::ordered_json::array();
        for (const Row& r : rows) {
            nlohmann::ordered_json o = nlohmann::ordered_json::object();
            for (const std::string& col : all_cols) {
                if (col == "Refs") o[col] = r.refs;
                else if (col == "Qty") o[col] = r.qty;
                else if (col == "Value") o[col] = r.value;
                else if (col == "Footprint") o[col] = r.footprint;
                else if (col == "MPN") o[col] = r.mpn;
                else o[col] = field_value(r, col);
            }
            arr.push_back(o);
        }
        return arr.dump(2);
    }

    // CSV (lineterminator="\n", QUOTE_MINIMAL)
    std::string out;
    for (size_t i = 0; i < all_cols.size(); ++i) {
        if (i) out += ",";
        out += csv_field(all_cols[i]);
    }
    out += "\n";
    for (const Row& r : rows) {
        for (size_t i = 0; i < all_cols.size(); ++i) {
            if (i) out += ",";
            const std::string& col = all_cols[i];
            std::string v;
            if (col == "Refs") v = r.refs;
            else if (col == "Qty") v = std::to_string(r.qty);
            else if (col == "Value") v = r.value;
            else if (col == "Footprint") v = r.footprint;
            else if (col == "MPN") v = r.mpn;
            else v = field_value(r, col);
            out += csv_field(v);
        }
        out += "\n";
    }
    return out;
}

// ────────────────────────────────────────────────────────── write_placement
namespace {
bool is_tht_only(const json& fp) {
    if (!fp.contains("pads") || !fp["pads"].is_array() || fp["pads"].empty()) return false;
    for (const auto& p : fp["pads"]) {
        std::string t = gets(p.value("type", json("smd")), "smd");
        if (t != "tht" && t != "np_tht") return false;
    }
    return true;
}
bool is_virtual(const json& fp) { return gets(fp.value("attr", json("smd")), "smd") == "virtual"; }
}  // namespace

std::string write_placement(const json& board, bool exclude_tht, bool exclude_virtual) {
    json origin = board.value("origin", json::array({0.0, 0.0}));
    double ox = getf(origin[0]), oy = getf(origin[1]);

    struct Row {
        std::string designator, midx, midy, layer, rotation;
    };
    std::vector<Row> rows;

    if (board.contains("footprints"))
        for (const auto& fp : board["footprints"]) {
            if (exclude_virtual && is_virtual(fp)) continue;
            if (exclude_tht && is_tht_only(fp)) continue;

            std::string ref = gets(fp.value("ref", json("?")), "?");
            json pos = fp.value("pos", json::array({0.0, 0.0}));
            double angle = fp.contains("angle") ? getf(fp["angle"]) : 0.0;
            std::string side = gets(fp.value("side", json("top")), "top");

            double mid_x = getf(pos[0]) - ox;
            double mid_y = getf(pos[1]) - oy;

            // Python: (-angle) % 360 / angle % 360 — Python modulo is floored (result
            // takes the sign of the divisor, i.e. always in [0,360)).
            double raw = (side == "bottom") ? -angle : angle;
            double rotation = std::fmod(raw, 360.0);
            if (rotation < 0) rotation += 360.0;

            std::string layer = (side == "top") ? "Top" : "Bottom";
            rows.push_back({ref, fmt_f(mid_x, 4), fmt_f(mid_y, 4), layer, fmt_f(rotation, 4)});
        }

    std::stable_sort(rows.begin(), rows.end(), [](const Row& a, const Row& b) {
        return nat_less(natural_key(a.designator), natural_key(b.designator));
    });

    std::string out = "Designator,MidX(mm),MidY(mm),Layer,Rotation\n";
    for (const Row& r : rows) {
        out += csv_field(r.designator) + "," + csv_field(r.midx) + "," + csv_field(r.midy) + "," +
               csv_field(r.layer) + "," + csv_field(r.rotation) + "\n";
    }
    return out;
}

}  // namespace chudware::ecad::fab
