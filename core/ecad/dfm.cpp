// DFM check suite implementation — see dfm.h. Mirrors engine/ecad/dfm.py exactly:
// rule catalogue, default thresholds, geometry approximations, violation record
// shape + the final deterministic ordering. OCCT-free; pure JSON/geometry.

#include "ecad/dfm.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "ecad/drc.h"     // same_net
#include "ecad/schema.h"  // pad_world, is_board

namespace chudware::ecad {

using nlohmann::json;

namespace {

constexpr double kEps = 1e-9;
constexpr double kPi = 3.14159265358979323846;

using Point = std::pair<double, double>;

double getf(const json& v, double dflt = 0.0) {
    return v.is_number() ? v.get<double>() : dflt;
}

// Python format(x, ".4g").
std::string fmt4g(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.4g", v);
    return std::string(buf);
}

// Python format(x, ".1f").
std::string fmt1f(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.1f", v);
    return std::string(buf);
}

// Python repr() of a string value: single-quoted.
std::string repr_str(const std::string& s) { return "'" + s + "'"; }

// Python repr() of a net value: string -> 'name'; null -> None.
std::string repr_net(const json& net) {
    if (net.is_string()) return "'" + net.get<std::string>() + "'";
    if (net.is_null()) return "None";
    return net.dump();
}

double dist(const Point& a, const Point& b) {
    return std::hypot(a.first - b.first, a.second - b.second);
}

double clamp01(double t) { return t < 0.0 ? 0.0 : (t > 1.0 ? 1.0 : t); }

double point_seg_dist(const Point& p, const Point& a, const Point& b) {
    double ax = a.first, ay = a.second, bx = b.first, by = b.second;
    double dx = bx - ax, dy = by - ay;
    double denom = dx * dx + dy * dy;
    if (denom <= kEps) return dist(p, a);
    double t = clamp01(((p.first - ax) * dx + (p.second - ay) * dy) / denom);
    Point proj{ax + t * dx, ay + t * dy};
    return dist(p, proj);
}

double orient(const Point& p, const Point& q, const Point& r) {
    return (q.first - p.first) * (r.second - p.second) -
           (q.second - p.second) * (r.first - p.first);
}

bool on_box(const Point& p, const Point& q, const Point& r) {
    return (std::min(p.first, q.first) - kEps <= r.first &&
            r.first <= std::max(p.first, q.first) + kEps &&
            std::min(p.second, q.second) - kEps <= r.second &&
            r.second <= std::max(p.second, q.second) + kEps);
}

bool segs_intersect(const Point& a, const Point& b, const Point& c, const Point& d) {
    double o1 = orient(a, b, c), o2 = orient(a, b, d);
    double o3 = orient(c, d, a), o4 = orient(c, d, b);
    if ((((o1 > kEps) != (o2 > kEps)) || ((o1 < -kEps) != (o2 < -kEps))) &&
        (((o3 > kEps) != (o4 > kEps)) || ((o3 < -kEps) != (o4 < -kEps)))) {
        if (((o1 > 0) != (o2 > 0)) && ((o3 > 0) != (o4 > 0))) return true;
    }
    if (std::abs(o1) <= kEps && on_box(a, b, c)) return true;
    if (std::abs(o2) <= kEps && on_box(a, b, d)) return true;
    if (std::abs(o3) <= kEps && on_box(c, d, a)) return true;
    if (std::abs(o4) <= kEps && on_box(c, d, b)) return true;
    return false;
}

double point_box_dist(const Point& p, double cx, double cy, double hx, double hy) {
    double dx = std::max(std::abs(p.first - cx) - hx, 0.0);
    double dy = std::max(std::abs(p.second - cy) - hy, 0.0);
    return std::hypot(dx, dy);
}

double seg_box_dist(const Point& a, const Point& b, double cx, double cy, double hx, double hy) {
    double x0 = cx - hx, x1 = cx + hx, y0 = cy - hy, y1 = cy + hy;
    Point corners[4] = {{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}};
    if ((x0 - kEps <= a.first && a.first <= x1 + kEps && y0 - kEps <= a.second &&
         a.second <= y1 + kEps) ||
        (x0 - kEps <= b.first && b.first <= x1 + kEps && y0 - kEps <= b.second &&
         b.second <= y1 + kEps))
        return 0.0;
    for (int i = 0; i < 4; ++i) {
        if (segs_intersect(a, b, corners[i], corners[(i + 1) % 4])) return 0.0;
    }
    double best = std::numeric_limits<double>::infinity();
    for (int i = 0; i < 4; ++i) best = std::min(best, point_seg_dist(corners[i], a, b));
    for (int i = 0; i < 4; ++i) {
        const Point& c = corners[i];
        const Point& dd = corners[(i + 1) % 4];
        best = std::min(best, std::min(point_seg_dist(a, c, dd), point_seg_dist(b, c, dd)));
    }
    return best;
}

double box_box_dist(const Point& c1, const Point& h1, const Point& c2, const Point& h2) {
    double dx = std::max(std::abs(c1.first - c2.first) - (h1.first + h2.first), 0.0);
    double dy = std::max(std::abs(c1.second - c2.second) - (h1.second + h2.second), 0.0);
    return std::hypot(dx, dy);
}

Point rotated_aabb_half(double w, double h, double angle_deg) {
    double r = angle_deg * kPi / 180.0;
    double c = std::abs(std::cos(r)), s = std::abs(std::sin(r));
    double hw = w / 2.0, hh = h / 2.0;
    return {hw * c + hh * s, hw * s + hh * c};
}

// ── default thresholds (dfm.py::_DEFAULTS) ───────────────────────────────────
double cfg(const json& rules, const std::string& key) {
    static const std::map<std::string, double> defaults = {
        {"silk_over_pad_min_gap", 0.0},
        {"silk_min_width", 0.15},
        {"silk_min_text", 0.8},
        {"mask_sliver_min", 0.1},
        {"acid_trap_angle_max", 90.0},
        // Guard band (deg) so a 90 deg orthogonal router joint never reads as an acid trap.
        {"acid_trap_guard_deg", 1.0},
        {"annular_min", 0.15},
        {"drill_min_via", 0.2},
        {"drill_min_pad", 0.15},
        {"drill_error_below", 0.1},
    };
    if (rules.is_object() && rules.contains(key) && rules[key].is_number())
        return rules[key].get<double>();
    return defaults.at(key);
}

// ── violation factory (dfm.py::_v) ──────────────────────────────────────────
json make_v(const std::string& rule, const std::string& severity,
            const std::vector<std::tuple<double, double, std::string>>& locs,
            std::vector<std::string> items, const std::string& message) {
    json jlocs = json::array();
    for (const auto& [x, y, layer] : locs) jlocs.push_back(json::array({x, y, layer}));
    std::sort(items.begin(), items.end());
    return json{{"rule", rule}, {"severity", severity}, {"locs", jlocs},
                {"items", items}, {"message", message}};
}

// world rotate: rotate (x,y) about origin, optional mirror in X, then translate.
Point world_rotate(double x, double y, double cx, double cy, double angle_deg, bool mirror_x) {
    if (mirror_x) x = -x;
    double r = angle_deg * kPi / 180.0;
    double rx = x * std::cos(r) - y * std::sin(r);
    double ry = x * std::sin(r) + y * std::cos(r);
    return {rx + cx, ry + cy};
}

// ── pad copper geometry (dfm.py::_pad_copper) ────────────────────────────────
struct PadCopper {
    std::string kind;  // "circle" | "box"
    Point center{0, 0};
    double radius = 0.0;  // circle
    Point half{0, 0};     // box
    std::string silk_layer;
};

PadCopper pad_copper(const json& fp, const json& pad) {
    PadCopper pc;
    auto [wx, wy] = schema::pad_world(fp, pad);
    double w = 0.0, h = 0.0;
    if (pad.contains("size") && pad["size"].is_array()) {
        const json& size = pad["size"];
        if (size.size() > 0) w = getf(size[0]);
        h = (size.size() > 1) ? getf(size[1]) : w;
    }
    std::string side = fp.value("side", std::string("top"));
    pc.silk_layer = (side == "top") ? "F.SilkS" : "B.SilkS";
    std::string shape = pad.value("shape", std::string("rect"));
    pc.center = {wx, wy};
    if (shape == "circle" || shape == "oval") {
        pc.kind = "circle";
        pc.radius = std::max(w, h) / 2.0;
        return pc;
    }
    double pad_angle = getf(pad.contains("angle") ? pad["angle"] : json(0.0)) +
                       getf(fp.contains("angle") ? fp["angle"] : json(0.0));
    pc.kind = "box";
    pc.half = rotated_aabb_half(w, h, pad_angle);
    return pc;
}

bool pad_has_mask_opening(const json& pad, const std::string& side) {
    std::string mask_layer = (side == "top") ? "F.Mask" : "B.Mask";
    if (pad.contains("layers") && pad["layers"].is_array()) {
        for (const auto& l : pad["layers"])
            if (l.is_string() && l.get<std::string>() == mask_layer) return true;
    }
    return false;
}

bool point_in_polygon(double px, double py, const std::vector<Point>& poly) {
    size_t n = poly.size();
    bool inside = false;
    size_t j = (n == 0) ? 0 : n - 1;
    for (size_t i = 0; i < n; ++i) {
        double xi = poly[i].first, yi = poly[i].second;
        double xj = poly[j].first, yj = poly[j].second;
        if (((yi > py) != (yj > py)) &&
            (px < (xj - xi) * (py - yi) / (yj - yi + kEps) + xi))
            inside = !inside;
        j = i;
    }
    return inside;
}

// ── courtyard segment extraction (dfm.py::_courtyard_segments) ───────────────
std::vector<std::pair<Point, Point>> courtyard_segments(const json& fp,
                                                         const std::string& side_filter) {
    std::vector<std::pair<Point, Point>> segs;
    double cx = 0, cy = 0;
    if (fp.contains("pos") && fp["pos"].is_array()) {
        cx = getf(fp["pos"][0]);
        cy = getf(fp["pos"][1]);
    }
    double angle = getf(fp.contains("angle") ? fp["angle"] : json(0.0));
    bool mirror_x = fp.value("side", std::string("top")) == "bottom";
    auto W = [&](double x, double y) { return world_rotate(x, y, cx, cy, angle, mirror_x); };

    if (!fp.contains("graphics") || !fp["graphics"].is_array()) return segs;
    for (const auto& g : fp["graphics"]) {
        if (g.value("layer", std::string()) != side_filter) continue;
        std::string gtype = g.value("type", std::string("line"));
        if (gtype == "line") {
            Point p1{0, 0}, p2{0, 0};
            if (g.contains("p1") && g["p1"].is_array()) p1 = {getf(g["p1"][0]), getf(g["p1"][1])};
            if (g.contains("p2") && g["p2"].is_array()) p2 = {getf(g["p2"][0]), getf(g["p2"][1])};
            segs.push_back({W(p1.first, p1.second), W(p2.first, p2.second)});
        } else if (gtype == "rect") {
            Point p1{0, 0}, p2{0, 0};
            if (g.contains("p1") && g["p1"].is_array()) p1 = {getf(g["p1"][0]), getf(g["p1"][1])};
            if (g.contains("p2") && g["p2"].is_array()) p2 = {getf(g["p2"][0]), getf(g["p2"][1])};
            double x0 = p1.first, y0 = p1.second, x1 = p2.first, y1 = p2.second;
            Point corners[4] = {{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}};
            Point wc[4];
            for (int i = 0; i < 4; ++i) wc[i] = W(corners[i].first, corners[i].second);
            for (int i = 0; i < 4; ++i) segs.push_back({wc[i], wc[(i + 1) % 4]});
        } else if (gtype == "circle") {
            Point center{0, 0};
            if (g.contains("center") && g["center"].is_array())
                center = {getf(g["center"][0]), getf(g["center"][1])};
            double r = getf(g.contains("radius") ? g["radius"] : json(0.0));
            const int N = 24;
            std::vector<Point> pts;
            for (int i = 0; i < N; ++i) {
                double a = 2.0 * kPi * i / N;
                pts.push_back(W(center.first + r * std::cos(a), center.second + r * std::sin(a)));
            }
            for (int i = 0; i < N; ++i) segs.push_back({pts[i], pts[(i + 1) % N]});
        }
    }
    return segs;
}

// ── courtyard polygon (dfm.py::_courtyard_polygon) — greedy chain ────────────
std::vector<Point> courtyard_polygon(const json& fp, const std::string& side_filter) {
    auto segs = courtyard_segments(fp, side_filter);
    if (segs.empty()) return {};
    std::vector<bool> used(segs.size(), false);
    std::vector<Point> poly{segs[0].first, segs[0].second};
    used[0] = true;
    for (size_t step = 0; step + 1 < segs.size(); ++step) {
        Point tail = poly.back();
        long best_idx = -1;
        bool best_rev = false;
        double best_d = std::numeric_limits<double>::infinity();
        for (size_t k = 0; k < segs.size(); ++k) {
            if (used[k]) continue;
            double d0 = dist(tail, segs[k].first);
            double d1 = dist(tail, segs[k].second);
            if (d0 < best_d) {
                best_d = d0;
                best_idx = (long)k;
                best_rev = false;
            }
            if (d1 < best_d) {
                best_d = d1;
                best_idx = (long)k;
                best_rev = true;
            }
        }
        if (best_idx < 0) break;
        used[best_idx] = true;
        const auto& seg = segs[best_idx];
        poly.push_back(best_rev ? seg.first : seg.second);
    }
    return poly;
}

// ── Rule: silk_over_pad ──────────────────────────────────────────────────────
void check_silk_over_pad(const json& board, const json& rules, json& out) {
    double gap_min = cfg(rules, "silk_over_pad_min_gap");

    // collect silk graphics (board-level then footprint-level, in document order).
    struct SilkItem {
        json g;
        const json* fp;  // nullptr for board-level
    };
    std::vector<SilkItem> silk_items;
    if (board.contains("graphics") && board["graphics"].is_array()) {
        for (const auto& g : board["graphics"]) {
            std::string lyr = g.value("layer", std::string());
            if (lyr == "F.SilkS" || lyr == "B.SilkS") silk_items.push_back({g, nullptr});
        }
    }
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (!fp.contains("graphics") || !fp["graphics"].is_array()) continue;
            for (const auto& g : fp["graphics"]) {
                std::string lyr = g.value("layer", std::string());
                if (lyr == "F.SilkS" || lyr == "B.SilkS") silk_items.push_back({g, &fp});
            }
        }
    }

    // collect pads with mask openings.
    struct PadEntry {
        std::string silk_layer;
        PadCopper pc;
        std::string pad_id;
    };
    std::vector<PadEntry> pad_entries;
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            std::string side = fp.value("side", std::string("top"));
            std::string silk_layer = (side == "top") ? "F.SilkS" : "B.SilkS";
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                if (!pad_has_mask_opening(pad, side)) continue;
                PadEntry pe;
                pe.silk_layer = silk_layer;
                pe.pc = pad_copper(fp, pad);
                pe.pad_id = pad.value("id", std::string());
                pad_entries.push_back(std::move(pe));
            }
        }
    }
    if (pad_entries.empty()) return;

    for (const auto& si : silk_items) {
        const json& g = si.g;
        std::string g_layer = g.value("layer", std::string());
        std::string gtype = g.value("type", std::string("line"));
        std::string g_id = g.value("id", std::string());
        const json* fp_host = si.fp;

        auto silk_world = [&](double x, double y) -> Point {
            if (fp_host == nullptr) return {x, y};
            double cx = 0, cy = 0;
            if (fp_host->contains("pos") && (*fp_host)["pos"].is_array()) {
                cx = getf((*fp_host)["pos"][0]);
                cy = getf((*fp_host)["pos"][1]);
            }
            double angle = getf(fp_host->contains("angle") ? (*fp_host)["angle"] : json(0.0));
            bool mirror_x = fp_host->value("side", std::string("top")) == "bottom";
            return world_rotate(x, y, cx, cy, angle, mirror_x);
        };

        std::vector<std::pair<Point, Point>> w_segs;
        if (gtype == "line") {
            Point p1{0, 0}, p2{0, 0};
            if (g.contains("p1") && g["p1"].is_array()) p1 = {getf(g["p1"][0]), getf(g["p1"][1])};
            if (g.contains("p2") && g["p2"].is_array()) p2 = {getf(g["p2"][0]), getf(g["p2"][1])};
            w_segs.push_back({silk_world(p1.first, p1.second), silk_world(p2.first, p2.second)});
        } else if (gtype == "rect") {
            Point p1{0, 0}, p2{0, 0};
            if (g.contains("p1") && g["p1"].is_array()) p1 = {getf(g["p1"][0]), getf(g["p1"][1])};
            if (g.contains("p2") && g["p2"].is_array()) p2 = {getf(g["p2"][0]), getf(g["p2"][1])};
            Point corners[4] = {{p1.first, p1.second},
                                {p2.first, p1.second},
                                {p2.first, p2.second},
                                {p1.first, p2.second}};
            Point wc[4];
            for (int i = 0; i < 4; ++i) wc[i] = silk_world(corners[i].first, corners[i].second);
            for (int i = 0; i < 4; ++i) w_segs.push_back({wc[i], wc[(i + 1) % 4]});
        } else if (gtype == "text" || gtype == "reference" || gtype == "value") {
            Point pos{0, 0};
            if (g.contains("pos") && g["pos"].is_array()) pos = {getf(g["pos"][0]), getf(g["pos"][1])};
            Point wp = silk_world(pos.first, pos.second);
            w_segs.push_back({wp, wp});
        } else if (gtype == "circle") {
            Point center{0, 0};
            if (g.contains("center") && g["center"].is_array())
                center = {getf(g["center"][0]), getf(g["center"][1])};
            double r = getf(g.contains("radius") ? g["radius"] : json(0.0));
            const int N = 24;
            std::vector<Point> pts;
            for (int i = 0; i < N; ++i) {
                double a = 2.0 * kPi * i / N;
                pts.push_back(silk_world(center.first + r * std::cos(a),
                                         center.second + r * std::sin(a)));
            }
            for (int i = 0; i < N; ++i) w_segs.push_back({pts[i], pts[(i + 1) % N]});
        }

        if (w_segs.empty()) continue;

        for (const auto& pe : pad_entries) {
            if (pe.silk_layer != g_layer) continue;
            if (pe.pc.kind == "circle") {
                double r = pe.pc.radius;
                double cx_p = pe.pc.center.first, cy_p = pe.pc.center.second;
                for (const auto& [sa, sb] : w_segs) {
                    double d = point_seg_dist(pe.pc.center, sa, sb);
                    double gap = d - r;
                    if (gap < gap_min + kEps) {
                        out.push_back(make_v(
                            "silk_over_pad", "warning", {{cx_p, cy_p, g_layer}},
                            {g_id, pe.pad_id},
                            "silk graphic " + repr_str(g_id) + " on " + g_layer +
                                " overlaps exposed pad " + repr_str(pe.pad_id) + " (gap " +
                                fmt4g(gap) + "mm)"));
                        break;
                    }
                }
            } else {
                double hx = pe.pc.half.first, hy = pe.pc.half.second;
                double cx_p = pe.pc.center.first, cy_p = pe.pc.center.second;
                for (const auto& [sa, sb] : w_segs) {
                    double d = seg_box_dist(sa, sb, cx_p, cy_p, hx, hy);
                    if (d < gap_min + kEps) {
                        out.push_back(make_v(
                            "silk_over_pad", "warning", {{cx_p, cy_p, g_layer}},
                            {g_id, pe.pad_id},
                            "silk graphic " + repr_str(g_id) + " on " + g_layer +
                                " overlaps exposed pad " + repr_str(pe.pad_id) + " (gap " +
                                fmt4g(d) + "mm)"));
                        break;
                    }
                }
            }
        }
    }
}

// ── Rule: silk_min_width ─────────────────────────────────────────────────────
void check_silk_min_width(const json& board, const json& rules, json& out) {
    double min_w = cfg(rules, "silk_min_width");
    std::vector<json> all_graphics;
    if (board.contains("graphics") && board["graphics"].is_array())
        for (const auto& g : board["graphics"]) all_graphics.push_back(g);
    if (board.contains("footprints") && board["footprints"].is_array())
        for (const auto& fp : board["footprints"])
            if (fp.contains("graphics") && fp["graphics"].is_array())
                for (const auto& g : fp["graphics"]) all_graphics.push_back(g);

    for (const auto& g : all_graphics) {
        std::string lyr = g.value("layer", std::string());
        if (lyr != "F.SilkS" && lyr != "B.SilkS") continue;
        std::string typ = g.value("type", std::string());
        if (typ != "line" && typ != "rect" && typ != "circle" && typ != "arc") continue;
        // w = float(g.get("width", g.get("stroke_width", 0.0)) or 0.0)
        double w = 0.0;
        if (g.contains("width") && !g["width"].is_null())
            w = getf(g["width"]);
        else if (g.contains("stroke_width") && !g["stroke_width"].is_null())
            w = getf(g["stroke_width"]);
        if (w < min_w - kEps) {
            // pos = g.get("p1") or g.get("center") or g.get("pos") or [0,0]
            Point pos{0, 0};
            const json* p = nullptr;
            if (g.contains("p1") && g["p1"].is_array() && !g["p1"].empty()) p = &g["p1"];
            else if (g.contains("center") && g["center"].is_array() && !g["center"].empty())
                p = &g["center"];
            else if (g.contains("pos") && g["pos"].is_array() && !g["pos"].empty()) p = &g["pos"];
            if (p) pos = {getf((*p)[0]), getf((*p)[1])};
            std::string layer = g.value("layer", std::string("F.SilkS"));
            std::string gid = g.value("id", std::string());
            out.push_back(make_v(
                "silk_min_width", "warning", {{pos.first, pos.second, layer}}, {gid},
                "silk graphic " + repr_str(gid) + " stroke width " + fmt4g(w) +
                    "mm < fab minimum " + fmt4g(min_w) + "mm"));
        }
    }
}

// ── Rule: silk_min_text ──────────────────────────────────────────────────────
void check_silk_min_text(const json& board, const json& rules, json& out) {
    double min_h = cfg(rules, "silk_min_text");
    std::vector<json> all_graphics;
    if (board.contains("graphics") && board["graphics"].is_array())
        for (const auto& g : board["graphics"]) all_graphics.push_back(g);
    if (board.contains("footprints") && board["footprints"].is_array())
        for (const auto& fp : board["footprints"])
            if (fp.contains("graphics") && fp["graphics"].is_array())
                for (const auto& g : fp["graphics"]) all_graphics.push_back(g);

    for (const auto& g : all_graphics) {
        std::string lyr = g.value("layer", std::string());
        if (lyr != "F.SilkS" && lyr != "B.SilkS") continue;
        std::string typ = g.value("type", std::string());
        if (typ != "text" && typ != "reference" && typ != "value") continue;
        // sz = g.get("size", g.get("height", 0.0))
        double h = 0.0;
        if (g.contains("size")) {
            const json& sz = g["size"];
            if (sz.is_array()) {
                if (sz.size() > 1) h = getf(sz[1]);
                else if (sz.size() == 1) h = getf(sz[0]);
                else h = 0.0;
            } else {
                h = getf(sz);
            }
        } else if (g.contains("height")) {
            h = getf(g["height"]);
        }
        if (h < min_h - kEps) {
            Point pos{0, 0};
            if (g.contains("pos") && g["pos"].is_array() && g["pos"].size() >= 2)
                pos = {getf(g["pos"][0]), getf(g["pos"][1])};
            std::string layer = g.value("layer", std::string("F.SilkS"));
            std::string gid = g.value("id", std::string());
            out.push_back(make_v(
                "silk_min_text", "warning", {{pos.first, pos.second, layer}}, {gid},
                "silk text " + repr_str(gid) + " height " + fmt4g(h) + "mm < fab minimum " +
                    fmt4g(min_h) + "mm"));
        }
    }
}

// ── Rule: mask_sliver ────────────────────────────────────────────────────────
struct MaskEntry {
    PadCopper pc;
    std::string cu_layer;
    std::string pad_id;
};

double pad_pair_gap(const MaskEntry& a, const MaskEntry& b) {
    if (a.pc.kind == "circle" && b.pc.kind == "circle")
        return std::max(dist(a.pc.center, b.pc.center) - a.pc.radius - b.pc.radius, 0.0);
    if (a.pc.kind == "circle" && b.pc.kind == "box") {
        double d = point_box_dist(a.pc.center, b.pc.center.first, b.pc.center.second,
                                  b.pc.half.first, b.pc.half.second);
        return std::max(d - a.pc.radius, 0.0);
    }
    if (a.pc.kind == "box" && b.pc.kind == "circle") return pad_pair_gap(b, a);
    return box_box_dist(a.pc.center, a.pc.half, b.pc.center, b.pc.half);
}

void check_mask_sliver(const json& board, const json& rules, json& out) {
    double sliver_min = cfg(rules, "mask_sliver_min");
    std::vector<MaskEntry> entries;
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            std::string side = fp.value("side", std::string("top"));
            std::string cu_layer = (side == "top") ? "F.Cu" : "B.Cu";
            std::string mask_layer = (side == "top") ? "F.Mask" : "B.Mask";
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                bool has_mask = false;
                if (pad.contains("layers") && pad["layers"].is_array())
                    for (const auto& l : pad["layers"])
                        if (l.is_string() && l.get<std::string>() == mask_layer) has_mask = true;
                if (!has_mask) continue;
                MaskEntry me;
                me.pc = pad_copper(fp, pad);
                me.cu_layer = cu_layer;
                me.pad_id = pad.value("id", std::string());
                entries.push_back(std::move(me));
            }
        }
    }
    for (size_t i = 0; i < entries.size(); ++i) {
        for (size_t j = i + 1; j < entries.size(); ++j) {
            const MaskEntry& a = entries[i];
            const MaskEntry& b = entries[j];
            if (a.cu_layer != b.cu_layer) continue;
            double gap = pad_pair_gap(a, b);
            if (gap < sliver_min - kEps) {
                std::string layer = a.cu_layer;
                double cx_a = a.pc.center.first, cy_a = a.pc.center.second;
                double cx_b = b.pc.center.first, cy_b = b.pc.center.second;
                out.push_back(make_v(
                    "mask_sliver", "warning",
                    {{cx_a, cy_a, layer}, {cx_b, cy_b, layer}}, {a.pad_id, b.pad_id},
                    "soldermask sliver " + fmt4g(gap) + "mm between pads " + repr_str(a.pad_id) +
                        " and " + repr_str(b.pad_id) + " (< " + fmt4g(sliver_min) + "mm minimum)"));
            }
        }
    }
}

// ── Rule: acid_trap ──────────────────────────────────────────────────────────
void check_acid_trap(const json& board, const json& rules, json& out) {
    double angle_max = cfg(rules, "acid_trap_angle_max");
    double guard = cfg(rules, "acid_trap_guard_deg");
    // Fire only on a genuinely acute joint: strictly below the threshold minus a guard band,
    // so a 90 deg orthogonal router corner (the common case) is never flagged as a trap.
    double fire_below = angle_max - guard;

    std::vector<json> seg_tracks;
    if (board.contains("tracks") && board["tracks"].is_array())
        for (const auto& t : board["tracks"])
            if (t.value("kind", std::string("segment")) == "segment") seg_tracks.push_back(t);
    if (seg_tracks.empty()) return;

    // endpoint key rounded to 1e-6 mm.
    auto key = [](const json& p) -> Point {
        double x = std::round(getf(p[0]) * 1e6) / 1e6;
        double y = std::round(getf(p[1]) * 1e6) / 1e6;
        return {x, y};
    };
    auto key_str = [](const Point& k) {
        char buf[128];
        std::snprintf(buf, sizeof(buf), "%.17g,%.17g", k.first, k.second);
        return std::string(buf);
    };

    // ep_map: endpoint key -> list of track indices (insertion order preserved).
    // Python uses defaultdict(list) iterated in insertion order; we mirror that.
    std::vector<std::string> ep_order;
    std::map<std::string, std::vector<size_t>> ep_map;
    auto push_ep = [&](const std::string& ks, size_t idx) {
        auto it = ep_map.find(ks);
        if (it == ep_map.end()) {
            ep_order.push_back(ks);
            ep_map[ks].push_back(idx);
        } else {
            it->second.push_back(idx);
        }
    };
    std::map<std::string, Point> ep_pt;
    for (size_t i = 0; i < seg_tracks.size(); ++i) {
        Point k1 = key(seg_tracks[i]["p1"]);
        Point k2 = key(seg_tracks[i]["p2"]);
        std::string s1 = key_str(k1), s2 = key_str(k2);
        ep_pt[s1] = k1;
        ep_pt[s2] = k2;
        push_ep(s1, i);
        push_ep(s2, i);
    }

    std::set<std::tuple<std::string, std::string, std::string>> seen_pairs;
    for (const auto& ks : ep_order) {
        const Point ep = ep_pt[ks];
        const auto& tracks_at_ep = ep_map[ks];
        if (tracks_at_ep.size() < 2) continue;
        for (size_t i = 0; i < tracks_at_ep.size(); ++i) {
            for (size_t j = i + 1; j < tracks_at_ep.size(); ++j) {
                const json& ta = seg_tracks[tracks_at_ep[i]];
                const json& tb = seg_tracks[tracks_at_ep[j]];
                std::string ida = ta.value("id", std::string());
                std::string idb = tb.value("id", std::string());
                std::string lo = std::min(ida, idb), hi = std::max(ida, idb);
                auto pkey = std::make_tuple(lo, hi, ks);
                if (seen_pairs.count(pkey)) continue;
                seen_pairs.insert(pkey);
                json neta = ta.contains("net") ? ta["net"] : json(nullptr);
                json netb = tb.contains("net") ? tb["net"] : json(nullptr);
                if (!same_net(neta, netb)) continue;

                auto dir_from = [&](const json& t) -> Point {
                    Point kp1 = key(t["p1"]);
                    double dx, dy;
                    if (std::abs(kp1.first - ep.first) < kEps &&
                        std::abs(kp1.second - ep.second) < kEps) {
                        dx = getf(t["p2"][0]) - getf(t["p1"][0]);
                        dy = getf(t["p2"][1]) - getf(t["p1"][1]);
                    } else {
                        dx = getf(t["p1"][0]) - getf(t["p2"][0]);
                        dy = getf(t["p1"][1]) - getf(t["p2"][1]);
                    }
                    double len = std::hypot(dx, dy);
                    if (len < kEps) return {0.0, 0.0};
                    return {dx / len, dy / len};
                };
                Point da = dir_from(ta);
                Point db = dir_from(tb);
                if ((da.first == 0.0 && da.second == 0.0) ||
                    (db.first == 0.0 && db.second == 0.0))
                    continue;
                double dot = std::max(-1.0, std::min(1.0, da.first * db.first + da.second * db.second));
                double angle_deg = std::acos(dot) * 180.0 / kPi;
                if (angle_deg < fire_below - kEps) {
                    std::string layer = ta.value("layer", std::string("F.Cu"));
                    out.push_back(make_v(
                        "acid_trap", "warning", {{ep.first, ep.second, layer}}, {ida, idb},
                        "acid trap: tracks " + repr_str(ida) + " and " + repr_str(idb) +
                            " meet at " + fmt1f(angle_deg) + "deg on net " + repr_net(neta) +
                            " (acute, < " + fmt4g(fire_below) + "deg)"));
                }
            }
        }
    }
}

// ── Rule: courtyard_overlap ──────────────────────────────────────────────────
void check_courtyard_overlap(const json& board, const json& rules, json& out) {
    (void)rules;
    if (!board.contains("footprints") || !board["footprints"].is_array()) return;
    const json& fps = board["footprints"];
    if (fps.size() < 2) return;

    struct CyData {
        const json* fp;
        std::string layer;
        std::vector<std::pair<Point, Point>> segs;
        std::vector<Point> poly;
    };
    std::vector<CyData> cy_data;
    for (const auto& fp : fps) {
        std::string layer = fp.value("side", std::string("top")) == "top" ? "F.CrtYd" : "B.CrtYd";
        auto segs = courtyard_segments(fp, layer);
        if (segs.empty()) continue;
        CyData cd;
        cd.fp = &fp;
        cd.layer = layer;
        cd.segs = std::move(segs);
        cd.poly = courtyard_polygon(fp, layer);
        cy_data.push_back(std::move(cd));
    }

    for (size_t i = 0; i < cy_data.size(); ++i) {
        for (size_t j = i + 1; j < cy_data.size(); ++j) {
            const CyData& ca = cy_data[i];
            const CyData& cb = cy_data[j];
            if (ca.layer != cb.layer) continue;
            bool overlap = false;
            for (const auto& sa : ca.segs) {
                for (const auto& sb : cb.segs) {
                    if (segs_intersect(sa.first, sa.second, sb.first, sb.second)) {
                        overlap = true;
                        break;
                    }
                }
                if (overlap) break;
            }
            if (!overlap && !ca.poly.empty() && !cb.poly.empty()) {
                if (point_in_polygon(ca.poly[0].first, ca.poly[0].second, cb.poly))
                    overlap = true;
                else if (point_in_polygon(cb.poly[0].first, cb.poly[0].second, ca.poly))
                    overlap = true;
            }
            if (overlap) {
                const json& fp_a = *ca.fp;
                const json& fp_b = *cb.fp;
                double ax = 0, ay = 0, bx = 0, by = 0;
                if (fp_a.contains("pos") && fp_a["pos"].is_array()) {
                    ax = getf(fp_a["pos"][0]);
                    ay = getf(fp_a["pos"][1]);
                }
                if (fp_b.contains("pos") && fp_b["pos"].is_array()) {
                    bx = getf(fp_b["pos"][0]);
                    by = getf(fp_b["pos"][1]);
                }
                std::string layer = ca.layer;
                std::string id_a = fp_a.value("id", std::string());
                std::string id_b = fp_b.value("id", std::string());
                // ref defaults to id when absent.
                std::string ref_a = fp_a.contains("ref") && fp_a["ref"].is_string()
                                        ? fp_a["ref"].get<std::string>()
                                        : id_a;
                std::string ref_b = fp_b.contains("ref") && fp_b["ref"].is_string()
                                        ? fp_b["ref"].get<std::string>()
                                        : id_b;
                out.push_back(make_v(
                    "courtyard_overlap", "error", {{ax, ay, layer}, {bx, by, layer}},
                    {id_a, id_b},
                    "courtyard overlap: footprints " + repr_str(ref_a) + " and " +
                        repr_str(ref_b) + " courtyards intersect"));
            }
        }
    }
}

// ── Rule: annular_min ────────────────────────────────────────────────────────
void check_annular_min(const json& board, const json& rules, json& out) {
    double ar_min = cfg(rules, "annular_min");
    if (board.contains("vias") && board["vias"].is_array()) {
        for (const auto& v : board["vias"]) {
            double dia = getf(v.contains("diameter") ? v["diameter"] : json(0.0));
            double drill = getf(v.contains("drill") ? v["drill"] : json(0.0));
            double ring = (dia - drill) / 2.0;
            if (ring < ar_min - kEps) {
                Point at{0, 0};
                if (v.contains("at") && v["at"].is_array()) at = {getf(v["at"][0]), getf(v["at"][1])};
                std::string vid = v.value("id", std::string());
                out.push_back(make_v(
                    "annular_min", "warning", {{at.first, at.second, "F.Cu"}}, {vid},
                    "via " + repr_str(vid) + " annular ring " + fmt4g(ring) + "mm < DFM minimum " +
                        fmt4g(ar_min) + "mm"));
            }
        }
    }
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                if (!pad.contains("drill") || pad["drill"].is_null()) continue;
                const json& drill_info = pad["drill"];
                json dia_d;
                if (drill_info.is_object())
                    dia_d = drill_info.contains("diameter") ? drill_info["diameter"] : json(nullptr);
                else
                    dia_d = drill_info;
                if (dia_d.is_null()) continue;
                // size = pad.get("size") or [0,0]; pad_min_dim = min(s0,s1) if len>=2 else s0
                double s0 = 0, s1 = 0;
                bool have2 = false;
                if (pad.contains("size") && pad["size"].is_array()) {
                    const json& size = pad["size"];
                    if (size.size() > 0) s0 = getf(size[0]);
                    if (size.size() >= 2) {
                        s1 = getf(size[1]);
                        have2 = true;
                    }
                }
                double pad_min_dim = have2 ? std::min(s0, s1) : s0;
                double ring = (pad_min_dim - getf(dia_d)) / 2.0;
                if (ring < ar_min - kEps) {
                    auto [wx, wy] = schema::pad_world(fp, pad);
                    std::string pid = pad.value("id", std::string());
                    out.push_back(make_v(
                        "annular_min", "warning", {{wx, wy, "F.Cu"}}, {pid},
                        "pad " + repr_str(pid) + " annular ring " + fmt4g(ring) +
                            "mm < DFM minimum " + fmt4g(ar_min) + "mm"));
                }
            }
        }
    }
}

// ── Rule: drill_min ──────────────────────────────────────────────────────────
void check_drill_min(const json& board, const json& rules, json& out) {
    double via_min = cfg(rules, "drill_min_via");
    double pad_min = cfg(rules, "drill_min_pad");
    double err_below = cfg(rules, "drill_error_below");

    if (board.contains("vias") && board["vias"].is_array()) {
        for (const auto& v : board["vias"]) {
            double d = getf(v.contains("drill") ? v["drill"] : json(0.0));
            if (d < via_min - kEps) {
                std::string sev = d < err_below - kEps ? "error" : "warning";
                Point at{0, 0};
                if (v.contains("at") && v["at"].is_array()) at = {getf(v["at"][0]), getf(v["at"][1])};
                std::string vid = v.value("id", std::string());
                out.push_back(make_v(
                    "drill_min", sev, {{at.first, at.second, "F.Cu"}}, {vid},
                    "via " + repr_str(vid) + " drill " + fmt4g(d) + "mm < DFM minimum " +
                        fmt4g(via_min) + "mm"));
            }
        }
    }
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                if (!pad.contains("drill") || pad["drill"].is_null()) continue;
                const json& drill_info = pad["drill"];
                json dia;
                if (drill_info.is_object())
                    dia = drill_info.contains("diameter") ? drill_info["diameter"] : json(nullptr);
                else
                    dia = drill_info;
                if (dia.is_null()) continue;
                double d = getf(dia);
                if (d < pad_min - kEps) {
                    std::string sev = d < err_below - kEps ? "error" : "warning";
                    auto [wx, wy] = schema::pad_world(fp, pad);
                    std::string pid = pad.value("id", std::string());
                    out.push_back(make_v(
                        "drill_min", sev, {{wx, wy, "F.Cu"}}, {pid},
                        "pad " + repr_str(pid) + " drill " + fmt4g(d) + "mm < DFM minimum " +
                            fmt4g(pad_min) + "mm"));
                }
            }
        }
    }
}

}  // namespace

json run_dfm(const json& board, const json& rules) {
    json out = json::array();
    if (!schema::is_board(board)) return out;

    check_silk_over_pad(board, rules, out);
    check_silk_min_width(board, rules, out);
    check_silk_min_text(board, rules, out);
    check_mask_sliver(board, rules, out);
    check_acid_trap(board, rules, out);
    check_courtyard_overlap(board, rules, out);
    check_annular_min(board, rules, out);
    check_drill_min(board, rules, out);

    // deterministic final ordering: (rule, severity, items, locs, message)
    std::stable_sort(out.begin(), out.end(), [](const json& a, const json& b) {
        if (a["rule"] != b["rule"]) return a["rule"].get<std::string>() < b["rule"].get<std::string>();
        if (a["severity"] != b["severity"])
            return a["severity"].get<std::string>() < b["severity"].get<std::string>();
        const json& ai = a["items"];
        const json& bi = b["items"];
        for (size_t i = 0; i < std::min(ai.size(), bi.size()); ++i)
            if (ai[i] != bi[i]) return ai[i].get<std::string>() < bi[i].get<std::string>();
        if (ai.size() != bi.size()) return ai.size() < bi.size();
        const json& al = a["locs"];
        const json& bl = b["locs"];
        for (size_t i = 0; i < std::min(al.size(), bl.size()); ++i) {
            double ax = al[i][0].get<double>(), bx = bl[i][0].get<double>();
            if (ax != bx) return ax < bx;
            double ay = al[i][1].get<double>(), by = bl[i][1].get<double>();
            if (ay != by) return ay < by;
            std::string as = al[i][2].get<std::string>(), bs = bl[i][2].get<std::string>();
            if (as != bs) return as < bs;
        }
        if (al.size() != bl.size()) return al.size() < bl.size();
        return a["message"].get<std::string>() < b["message"].get<std::string>();
    });
    return out;
}

}  // namespace chudware::ecad
