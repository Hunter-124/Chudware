// Basic grid (Lee/A*) PCB autorouter — implementation. See autoroute.h.
// Ported feature-for-feature from engine/ecad/autoroute.py for byte-identical
// routed geometry. OCCT-free; pure JSON / grid / graph.

#include "ecad/autoroute.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <map>
#include <queue>
#include <regex>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "ecad/drc.h"
#include "ecad/schema.h"

namespace aicad::ecad {

using nlohmann::json;

namespace {

// ── tunables (mirror autoroute.py) ─────────────────────────────────────────────
constexpr double DEFAULT_PITCH = 0.25;
constexpr long MAX_GRID_CELLS = 240000;
constexpr double VIA_COST = 12.0;
constexpr double BEND_COST = 0.6;
constexpr double HV_BIAS = 0.35;
constexpr double DEFAULT_VIA_DIAMETER = 0.8;
constexpr double DEFAULT_VIA_DRILL = 0.4;
constexpr int DEFAULT_RIPUP_PASSES = 2;
constexpr int DEFAULT_RIPUP_BUDGET = 2;
constexpr double DEFAULT_DIFF_GAP = 0.2;
constexpr double DEFAULT_LENGTH_TOL = 0.2;
constexpr int MAX_MEANDER_TEETH = 200;

constexpr double kEps = 1e-9;

using Point = std::pair<double, double>;

double getf(const json& v, double dflt = 0.0) {
    return v.is_number() ? v.get<double>() : dflt;
}

double dist(const Point& a, const Point& b) {
    return std::hypot(a.first - b.first, a.second - b.second);
}

double clamp01(double t) { return t < 0.0 ? 0.0 : (t > 1.0 ? 1.0 : t); }

// Python round(): round-half-to-even (banker's rounding). std::nearbyint with the
// default FE_TONEAREST rounding mode does exactly this.
double py_round(double x) { return std::nearbyint(x); }

// Python round(x, ndigits) for ndigits>0. Python scales by 10^n, rounds half-even,
// then divides. Replicated with nearbyint after scaling.
double py_round_n(double x, int ndigits) {
    double scale = std::pow(10.0, ndigits);
    return std::nearbyint(x * scale) / scale;
}

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

double seg_seg_dist(const Point& a, const Point& b, const Point& c, const Point& d) {
    if (segs_intersect(a, b, c, d)) return 0.0;
    return std::min(std::min(point_seg_dist(a, c, d), point_seg_dist(b, c, d)),
                    std::min(point_seg_dist(c, a, b), point_seg_dist(d, a, b)));
}

double point_box_dist(const Point& p, double cx, double cy, double hx, double hy) {
    double dx = std::max(std::abs(p.first - cx) - hx, 0.0);
    double dy = std::max(std::abs(p.second - cy) - hy, 0.0);
    return std::hypot(dx, dy);
}

int n_cu_of(const json& board) {
    int n = 2;
    if (board.contains("stackup") && board["stackup"].is_object() &&
        board["stackup"].contains("copper_layers") && board["stackup"]["copper_layers"].is_number())
        n = board["stackup"]["copper_layers"].get<int>();
    return n;
}

std::vector<std::string> copper_layer_ids(const json& board) {
    return schema::copper_layer_ids(n_cu_of(board));
}

// ── net-class / width / clearance helpers (autoroute.py) ────────────────────────
json net_class(const json& board, const json& net) {
    const json* classes = nullptr;
    if (board.contains("net_classes") && board["net_classes"].is_object())
        classes = &board["net_classes"];
    json cls_name(nullptr);
    if (board.contains("nets") && board["nets"].is_array()) {
        for (const auto& n : board["nets"]) {
            if (n.is_object() && n.contains("name") && n["name"] == net) {
                cls_name = n.contains("class") ? n["class"] : json(nullptr);
                break;
            }
        }
    }
    if (classes && cls_name.is_string() && classes->contains(cls_name.get<std::string>()))
        return (*classes)[cls_name.get<std::string>()];
    if (classes && classes->contains("default")) return (*classes)["default"];
    return json::object();
}

double track_width(const json& board, const json& net) {
    json nc = net_class(board, net);
    if (nc.contains("track_width")) return getf(nc["track_width"]);
    double dflt = 0.15;
    if (board.contains("rules") && board["rules"].is_object() &&
        board["rules"].contains("track_width_min"))
        dflt = getf(board["rules"]["track_width_min"], 0.15);
    return dflt;
}

std::pair<double, double> via_dims(const json& board, const json& net) {
    json nc = net_class(board, net);
    double dia = nc.contains("via_diameter") ? getf(nc["via_diameter"]) : DEFAULT_VIA_DIAMETER;
    double drill = nc.contains("via_drill") ? getf(nc["via_drill"]) : DEFAULT_VIA_DRILL;
    return {dia, drill};
}

double clearance_of(const json& board, const json& net) {
    json nc = net_class(board, net);
    if (nc.contains("clearance")) return getf(nc["clearance"]);
    double dflt = 0.2;
    if (board.contains("rules") && board["rules"].is_object() &&
        board["rules"].contains("clearance"))
        dflt = getf(board["rules"]["clearance"], 0.2);
    return dflt;
}

double diff_gap_of(const json& board, const json& net, const json& override_) {
    if (!override_.is_null()) return getf(override_);
    json nc = net_class(board, net);
    if (nc.contains("diff_gap")) return getf(nc["diff_gap"]);
    return DEFAULT_DIFF_GAP;
}

// ── id counter (autoroute.py::_IdCounter) ───────────────────────────────────────
struct IdCounter {
    std::string prefix;
    long n = 0;
    IdCounter(const json& board, const std::string& pfx, const std::string& key) : prefix(pfx) {
        std::regex pat("^" + pfx + "([0-9]+)$");
        if (board.contains(key) && board[key].is_array()) {
            for (const auto& it : board[key]) {
                if (it.is_object() && it.contains("id") && it["id"].is_string()) {
                    std::smatch m;
                    std::string id = it["id"].get<std::string>();
                    if (std::regex_match(id, m, pat)) n = std::max(n, std::stol(m[1].str()));
                }
            }
        }
    }
    std::string next() {
        ++n;
        return prefix + std::to_string(n);
    }
};

// ── shape edge distance to a point (autoroute.py::_Grid._cell_clear_dist) ────────
double cell_clear_dist(const Shape& sh, const Point& p) {
    if (sh.kind == "circle") return std::max(dist(sh.center, p) - sh.margin, 0.0);
    if (sh.kind == "box")
        return point_box_dist(p, sh.center.first, sh.center.second, sh.half.first, sh.half.second);
    return std::max(point_seg_dist(p, sh.p1, sh.p2) - sh.margin, 0.0);
}

// shape derived from a routed track dict (drc.py::_track_shape). Returns false for
// non-segment tracks.
bool track_shape_of(const json& t, Shape& out) {
    if (t.value("kind", std::string("segment")) != "segment") return false;
    Point p1{0, 0}, p2{0, 0};
    if (t.contains("p1") && t["p1"].is_array()) p1 = {getf(t["p1"][0]), getf(t["p1"][1])};
    if (t.contains("p2") && t["p2"].is_array()) p2 = {getf(t["p2"][0]), getf(t["p2"][1])};
    double width = getf(t.contains("width") ? t["width"] : json(0.0));
    out.kind = "seg";
    out.net = t.contains("net") ? t["net"] : json(nullptr);
    out.layers.clear();
    if (t.contains("layer") && t["layer"].is_string())
        out.layers.insert(t["layer"].get<std::string>());
    else
        out.layers.insert(std::string());
    out.item_id = t.value("id", std::string());
    out.p1 = p1;
    out.p2 = p2;
    out.margin = width / 2.0;
    out.loc = {(p1.first + p2.first) / 2.0, (p1.second + p2.second) / 2.0};
    return true;
}

void via_shape_of(const json& v, Shape& out) {
    Point at{0, 0};
    if (v.contains("at") && v["at"].is_array()) at = {getf(v["at"][0]), getf(v["at"][1])};
    double radius = getf(v.contains("diameter") ? v["diameter"] : json(0.0)) / 2.0;
    out.kind = "circle";
    out.net = v.contains("net") ? v["net"] : json(nullptr);
    out.layers.clear();
    if (v.contains("layers") && v["layers"].is_array() && !v["layers"].empty()) {
        for (const auto& l : v["layers"])
            if (l.is_string()) out.layers.insert(l.get<std::string>());
    }
    if (out.layers.empty()) {
        out.layers.insert("F.Cu");
        out.layers.insert("B.Cu");
    }
    out.item_id = v.value("id", std::string());
    out.center = at;
    out.margin = radius;
    out.loc = at;
}

// ── outline segments (drc.py::_outline_segments, lines + arc/circle tessellation) ─
std::vector<std::pair<Point, Point>> outline_segments(const json& board) {
    std::vector<std::pair<Point, Point>> segs;
    if (!board.contains("outline") || !board["outline"].is_object()) return segs;
    const json& outline = board["outline"];
    if (!outline.contains("shapes") || !outline["shapes"].is_array()) return segs;
    constexpr double kPi = 3.14159265358979323846;
    for (const auto& shp : outline["shapes"]) {
        std::string typ = shp.value("type", std::string());
        if (typ == "line") {
            Point p1{0, 0}, p2{0, 0};
            if (shp.contains("p1") && shp["p1"].is_array())
                p1 = {getf(shp["p1"][0]), getf(shp["p1"][1])};
            if (shp.contains("p2") && shp["p2"].is_array())
                p2 = {getf(shp["p2"][0]), getf(shp["p2"][1])};
            segs.push_back({p1, p2});
        } else if (typ == "arc") {
            double cx = 0, cy = 0;
            if (shp.contains("center") && shp["center"].is_array()) {
                cx = getf(shp["center"][0]);
                cy = getf(shp["center"][1]);
            }
            double r = getf(shp.contains("radius") ? shp["radius"] : json(0.0));
            double a0 = getf(shp.contains("a0") ? shp["a0"] : json(0.0)) * kPi / 180.0;
            double a1 = getf(shp.contains("a1") ? shp["a1"] : json(0.0)) * kPi / 180.0;
            int n = std::max(8, (int)(std::abs(a1 - a0) / (kPi / 18.0)));
            bool have_prev = false;
            Point prev{0, 0};
            for (int i = 0; i <= n; ++i) {
                double a = a0 + (a1 - a0) * i / n;
                Point pt{cx + r * std::cos(a), cy + r * std::sin(a)};
                if (have_prev) segs.push_back({prev, pt});
                prev = pt;
                have_prev = true;
            }
        } else if (typ == "circle") {
            double cx = 0, cy = 0;
            if (shp.contains("center") && shp["center"].is_array()) {
                cx = getf(shp["center"][0]);
                cy = getf(shp["center"][1]);
            }
            double r = getf(shp.contains("radius") ? shp["radius"] : json(0.0));
            int n = 48;
            bool have_prev = false;
            Point prev{0, 0};
            for (int i = 0; i <= n; ++i) {
                double a = 2 * kPi * i / n;
                Point pt{cx + r * std::cos(a), cy + r * std::sin(a)};
                if (have_prev) segs.push_back({prev, pt});
                prev = pt;
                have_prev = true;
            }
        }
    }
    return segs;
}

// ── board bbox (autoroute.py::_board_bbox) ──────────────────────────────────────
std::tuple<double, double, double, double> board_bbox(const json& board) {
    std::vector<double> xs, ys;
    auto edges = outline_segments(board);
    for (const auto& [a, b] : edges) {
        xs.push_back(a.first);
        xs.push_back(b.first);
        ys.push_back(a.second);
        ys.push_back(b.second);
    }
    if (xs.empty()) {
        for (const auto& sh : copper_shapes(board)) {
            if (sh.kind == "circle") {
                xs.push_back(sh.center.first - sh.margin);
                xs.push_back(sh.center.first + sh.margin);
                ys.push_back(sh.center.second - sh.margin);
                ys.push_back(sh.center.second + sh.margin);
            } else if (sh.kind == "box") {
                xs.push_back(sh.center.first - sh.half.first);
                xs.push_back(sh.center.first + sh.half.first);
                ys.push_back(sh.center.second - sh.half.second);
                ys.push_back(sh.center.second + sh.half.second);
            } else {  // seg
                xs.push_back(sh.p1.first);
                xs.push_back(sh.p2.first);
                ys.push_back(sh.p1.second);
                ys.push_back(sh.p2.second);
            }
        }
        if (xs.empty()) return {0.0, 0.0, 1.0, 1.0};
        double pad = 2.0;
        double mnx = *std::min_element(xs.begin(), xs.end());
        double mxx = *std::max_element(xs.begin(), xs.end());
        double mny = *std::min_element(ys.begin(), ys.end());
        double mxy = *std::max_element(ys.begin(), ys.end());
        return {mnx - pad, mny - pad, mxx + pad, mxy + pad};
    }
    return {*std::min_element(xs.begin(), xs.end()), *std::min_element(ys.begin(), ys.end()),
            *std::max_element(xs.begin(), xs.end()), *std::max_element(ys.begin(), ys.end())};
}

bool point_in_outline(const Point& p, const std::vector<std::pair<Point, Point>>& edges) {
    double x = p.first, y = p.second;
    bool inside = false;
    for (const auto& [a, b] : edges) {
        double x1 = a.first, y1 = a.second, x2 = b.first, y2 = b.second;
        if ((y1 > y) != (y2 > y)) {
            double xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1);
            if (x < xint) inside = !inside;
        }
    }
    return inside;
}

// ── the two-layer obstacle grid (autoroute.py::_Grid) ───────────────────────────
struct Grid {
    const json& board;
    std::vector<std::string> layers;
    double minx, miny, maxx, maxy;
    double pitch;
    int nx, ny;
    std::vector<std::pair<Point, Point>> edges;
    double edge_clear;
    std::map<std::string, std::vector<Shape>> shapes_by_layer;
    std::vector<char> outline_mask;

    Grid(const json& b, double p) : board(b), pitch(p) {
        layers = copper_layer_ids(board);
        auto [a, c, e, f] = board_bbox(board);
        minx = a;
        miny = c;
        maxx = e;
        maxy = f;
        nx = std::max(1, (int)py_round((maxx - minx) / pitch) + 1);
        ny = std::max(1, (int)py_round((maxy - miny) / pitch) + 1);
        edges = outline_segments(board);
        edge_clear = 0.0;
        if (board.contains("rules") && board["rules"].is_object() &&
            board["rules"].contains("edge_clearance"))
            edge_clear = getf(board["rules"]["edge_clearance"]);
        index_shapes();
        build_outline_mask();
    }

    Point cell_xy(int ix, int iy) const {
        return {minx + ix * pitch, miny + iy * pitch};
    }
    std::pair<int, int> nearest_cell(const Point& p) const {
        int ix = (int)py_round((p.first - minx) / pitch);
        int iy = (int)py_round((p.second - miny) / pitch);
        ix = std::min(std::max(ix, 0), nx - 1);
        iy = std::min(std::max(iy, 0), ny - 1);
        return {ix, iy};
    }
    int idx(int ix, int iy) const { return iy * nx + ix; }

    void index_shapes() {
        for (const auto& ly : layers) shapes_by_layer[ly] = {};
        for (const auto& sh : copper_shapes(board)) {
            for (const auto& ly : sh.layers) {
                auto it = shapes_by_layer.find(ly);
                if (it != shapes_by_layer.end()) it->second.push_back(sh);
            }
        }
    }

    void build_outline_mask() {
        int n = nx * ny;
        if (edges.empty()) {
            outline_mask.assign(n, 1);
            return;
        }
        outline_mask.assign(n, 0);
        for (int iy = 0; iy < ny; ++iy) {
            for (int ix = 0; ix < nx; ++ix) {
                Point p = cell_xy(ix, iy);
                if (!point_in_outline(p, edges)) continue;
                if (edge_clear > 0) {
                    double d = std::numeric_limits<double>::infinity();
                    for (const auto& [a, b] : edges) d = std::min(d, point_seg_dist(p, a, b));
                    if (d < edge_clear) continue;
                }
                outline_mask[idx(ix, iy)] = 1;
            }
        }
    }

    // per-layer blocked map for `net` (other-net copper inflated by clearance+half-width).
    // ``halo`` is the copper inflation radius; pass < 0 to use the track halo (clearance + half
    // track width). The via halo (clearance + via_radius) is passed in for the via-placement map.
    std::vector<std::vector<char>> build_blocked(const json& net, double halo = -1.0) const {
        if (halo < 0.0) halo = clearance_of(board, net) + track_width(board, net) / 2.0;
        std::vector<std::vector<char>> blocked;
        for (const auto& ly : layers) {
            std::vector<char> row(outline_mask.size());
            for (size_t i = 0; i < outline_mask.size(); ++i) row[i] = outline_mask[i] ? 0 : 1;
            std::vector<Shape> shapes;
            auto it = shapes_by_layer.find(ly);
            if (it != shapes_by_layer.end()) {
                for (const auto& s : it->second)
                    if (!same_net(s.net, net)) shapes.push_back(s);
            }
            if (!shapes.empty()) mark_shapes(row, shapes, halo);
            blocked.push_back(std::move(row));
        }
        return blocked;
    }

    void mark_shapes(std::vector<char>& row, const std::vector<Shape>& shapes, double halo) const {
        for (const auto& sh : shapes) {
            double bx0, by0, bx1, by1;
            if (sh.kind == "circle") {
                bx0 = sh.center.first - sh.margin;
                by0 = sh.center.second - sh.margin;
                bx1 = sh.center.first + sh.margin;
                by1 = sh.center.second + sh.margin;
            } else if (sh.kind == "box") {
                bx0 = sh.center.first - sh.half.first;
                by0 = sh.center.second - sh.half.second;
                bx1 = sh.center.first + sh.half.first;
                by1 = sh.center.second + sh.half.second;
            } else {  // seg
                bx0 = std::min(sh.p1.first, sh.p2.first) - sh.margin;
                bx1 = std::max(sh.p1.first, sh.p2.first) + sh.margin;
                by0 = std::min(sh.p1.second, sh.p2.second) - sh.margin;
                by1 = std::max(sh.p1.second, sh.p2.second) + sh.margin;
            }
            int ix0 = std::max(0, (int)std::floor((bx0 - halo - minx) / pitch));
            int ix1 = std::min(nx - 1, (int)std::ceil((bx1 + halo - minx) / pitch));
            int iy0 = std::max(0, (int)std::floor((by0 - halo - miny) / pitch));
            int iy1 = std::min(ny - 1, (int)std::ceil((by1 + halo - miny) / pitch));
            for (int iy = iy0; iy <= iy1; ++iy) {
                int base = iy * nx;
                for (int ix = ix0; ix <= ix1; ++ix) {
                    if (row[base + ix]) continue;
                    if (cell_clear_dist(sh, cell_xy(ix, iy)) < halo) row[base + ix] = 1;
                }
            }
        }
    }
};

// ── routed-copper registry (autoroute.py::_RoutedRegistry) ──────────────────────
// net keys are JSON values; the oracle iterates `sorted(..., key=str)`. The router's
// nets here are always strings, so we key by the string and sort lexicographically.
struct RoutedRegistry {
    std::map<std::string, std::vector<json>> tracks;  // ordered (sorted by key=str)
    std::map<std::string, std::vector<json>> vias;
    std::map<std::string, int> ripup_count;

    void add(const std::string& net, const std::vector<json>& t, const std::vector<json>& v) {
        auto& tv = tracks[net];
        tv.insert(tv.end(), t.begin(), t.end());
        auto& vv = vias[net];
        vv.insert(vv.end(), v.begin(), v.end());
    }
    std::pair<std::vector<json>, std::vector<json>> pop(const std::string& net) {
        std::vector<json> t, v;
        auto it = tracks.find(net);
        if (it != tracks.end()) {
            t = it->second;
            tracks.erase(it);
        }
        auto iv = vias.find(net);
        if (iv != vias.end()) {
            v = iv->second;
            vias.erase(iv);
        }
        ripup_count[net] += 1;
        return {t, v};
    }
    // obstacle shapes from every OTHER net (current net is source/target, not obstacle).
    std::vector<Shape> shapes_for(const std::string& exclude_net) const {
        std::vector<Shape> out;
        for (const auto& [net, tv] : tracks) {  // std::map already key-sorted
            if (same_net(json(net), json(exclude_net))) continue;
            for (const auto& t : tv) {
                Shape sh;
                if (track_shape_of(t, sh)) out.push_back(sh);
            }
        }
        for (const auto& [net, vv] : vias) {
            if (same_net(json(net), json(exclude_net))) continue;
            for (const auto& v : vv) {
                Shape sh;
                via_shape_of(v, sh);
                out.push_back(sh);
            }
        }
        return out;
    }
    std::vector<json> all_tracks() const {
        std::vector<json> out;
        for (const auto& [net, tv] : tracks) out.insert(out.end(), tv.begin(), tv.end());
        return out;
    }
    std::vector<json> all_vias() const {
        std::vector<json> out;
        for (const auto& [net, vv] : vias) out.insert(out.end(), vv.begin(), vv.end());
        return out;
    }
};

// ── A* over the two-layer cell graph (autoroute.py::_astar) ─────────────────────
using Node = std::tuple<int, int, int>;  // (layer, ix, iy)

// directional state of how a node was entered: 0 = None (via or start), else a
// direction index 1..4 mapping to moves[] +1.  We store dx,dy in came for emit.
struct Came {
    bool has_prev;
    Node prev;
    bool has_dir;  // dir is None when this is a via or the start
    int dx, dy;
};

std::vector<Node> reconstruct(const std::map<Node, Came>& came, Node node) {
    std::vector<Node> path{node};
    auto it = came.find(node);
    while (it != came.end() && it->second.has_prev) {
        node = it->second.prev;
        path.push_back(node);
        it = came.find(node);
    }
    std::reverse(path.begin(), path.end());
    return path;
}

bool astar(const Grid& grid, const std::vector<std::vector<char>>& blocked,
           const std::set<Node>& starts, const std::set<Node>& goals, const Point& goal_xy,
           double via_cost, double bend_cost, std::vector<Node>& out_path,
           const std::vector<std::vector<char>>* via_blocked = nullptr) {
    auto [gix, giy] = grid.nearest_cell(goal_xy);
    auto h = [&](int /*layer*/, int ix, int iy) -> double {
        return std::abs(ix - gix) + std::abs(iy - giy);
    };

    std::map<Node, Came> came;
    std::map<Node, double> gscore;

    // heap entry mirrors Python (f, g, node, dir). Python orders by (f, g, node)
    // — dir never breaks a tie between distinct heap entries (the node tuple is
    // already a total order). We keep a push counter only to make the priority
    // queue a stable min-heap on (f, g, node); since (f,g,node) is itself a total
    // order over distinct entries, the counter is just a deterministic fallback.
    struct HeapEntry {
        double f, g;
        Node node;
        bool has_dir;
        int dx, dy;
    };
    struct Cmp {
        bool operator()(const HeapEntry& a, const HeapEntry& b) const {
            // min-heap: return true if a has LOWER priority than b (a after b).
            if (a.f != b.f) return a.f > b.f;
            if (a.g != b.g) return a.g > b.g;
            return a.node > b.node;  // tuple ordering, matches Python (layer,ix,iy)
        }
    };
    std::priority_queue<HeapEntry, std::vector<HeapEntry>, Cmp> heap;

    // sorted(starts): std::set<Node> already iterates in tuple order.
    for (const auto& s : starts) {
        if (blocked[std::get<0>(s)][grid.idx(std::get<1>(s), std::get<2>(s))]) continue;
        gscore[s] = 0.0;
        heap.push({h(std::get<0>(s), std::get<1>(s), std::get<2>(s)), 0.0, s, false, 0, 0});
        came[s] = Came{false, Node{}, false, 0, 0};
    }

    const int moves[4][2] = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};
    int n_layers = (int)grid.layers.size();

    while (!heap.empty()) {
        HeapEntry top = heap.top();
        heap.pop();
        Node node = top.node;
        double g = top.g;
        auto git = gscore.find(node);
        if (git != gscore.end() && g > git->second) continue;
        if (g > (git == gscore.end() ? std::numeric_limits<double>::infinity() : git->second))
            continue;
        if (goals.count(node)) {
            out_path = reconstruct(came, node);
            return true;
        }
        int layer = std::get<0>(node), ix = std::get<1>(node), iy = std::get<2>(node);
        const Came& cn = came[node];
        bool prev_has_dir = cn.has_dir;
        int pdx = cn.dx, pdy = cn.dy;
        // same-layer orthogonal moves
        for (int mi = 0; mi < 4; ++mi) {
            int dx = moves[mi][0], dy = moves[mi][1];
            int nx_ = ix + dx, ny_ = iy + dy;
            if (nx_ < 0 || ny_ < 0 || nx_ >= grid.nx || ny_ >= grid.ny) continue;
            if (blocked[layer][grid.idx(nx_, ny_)]) continue;
            double step = 1.0;
            if (prev_has_dir && (pdx != dx || pdy != dy)) step += bend_cost;
            if (layer == 0 && dy != 0)
                step += HV_BIAS;
            else if (layer == n_layers - 1 && dx != 0)
                step += HV_BIAS;
            Node nb{layer, nx_, ny_};
            double ng = g + step;
            auto nit = gscore.find(nb);
            double cur = nit == gscore.end() ? std::numeric_limits<double>::infinity() : nit->second;
            if (ng < cur) {
                gscore[nb] = ng;
                came[nb] = Came{true, node, true, dx, dy};
                heap.push({ng + h(layer, nx_, ny_), ng, nb, true, dx, dy});
            }
        }
        // layer change (via) at this cell. A through-via occupies the SAME cell on every copper
        // layer and its copper radius (via_dia/2) dwarfs a track's half-width, so the track-halo
        // `blocked` map is not enough: gate the via on the via-halo map (`via_blocked`) across ALL
        // layers, else the placed via lands within `clearance` of other-net copper (a DRC violation).
        bool via_ok_here = true;
        if (via_blocked) {
            for (int L = 0; L < n_layers; ++L)
                if ((*via_blocked)[L][grid.idx(ix, iy)]) {
                    via_ok_here = false;
                    break;
                }
        }
        for (int nl = 0; nl < n_layers; ++nl) {
            if (nl == layer) continue;
            if (blocked[nl][grid.idx(ix, iy)]) continue;
            if (!via_ok_here) continue;
            Node nb{nl, ix, iy};
            double ng = g + via_cost;
            auto nit = gscore.find(nb);
            double cur = nit == gscore.end() ? std::numeric_limits<double>::infinity() : nit->second;
            if (ng < cur) {
                gscore[nb] = ng;
                came[nb] = Came{true, node, false, 0, 0};  // None dir = via
                // pushed dir carries prev_dir (used only for ordering; irrelevant)
                heap.push({ng + h(nl, ix, iy), ng, nb, prev_has_dir, pdx, pdy});
            }
        }
    }
    return false;
}

int sign(double v) { return (v > 1e-9) - (v < -1e-9); }

json mk_track(const std::string& tid, const Point& p1, const Point& p2, const json& net,
              double width, const std::string& layer) {
    return json{{"id", tid},
                {"net", net},
                {"layer", layer},
                {"width", width},
                {"kind", "segment"},
                {"p1", json::array({p1.first, p1.second})},
                {"p2", json::array({p2.first, p2.second})}};
}

// emit merged colinear segment tracks for a run of grid points (autoroute.py::_emit_run).
void emit_run(const std::vector<Point>& pts, const std::string& layer, const json& net, double width,
              std::vector<json>& tracks, IdCounter& tcnt) {
    if (pts.size() < 2) return;
    Point seg_start = pts[0];
    Point prev = pts[0];
    bool have_dir = false;
    std::pair<int, int> cur_dir{0, 0};
    for (size_t i = 1; i < pts.size(); ++i) {
        const Point& p = pts[i];
        std::pair<int, int> d{sign(p.first - prev.first), sign(p.second - prev.second)};
        if (!have_dir) {
            cur_dir = d;
            have_dir = true;
        } else if (d != cur_dir) {
            tracks.push_back(mk_track(tcnt.next(), seg_start, prev, net, width, layer));
            seg_start = prev;
            cur_dir = d;
        }
        prev = p;
    }
    if (seg_start != prev) tracks.push_back(mk_track(tcnt.next(), seg_start, prev, net, width, layer));
}

// compress a cell path into tracks + vias (autoroute.py::_path_to_geometry).
std::pair<std::vector<json>, std::vector<json>> path_to_geometry(
    const Grid& grid, const std::vector<Node>& path, const json& net, double width, IdCounter& tcnt,
    IdCounter& vcnt, double via_dia, double via_drill, const Point& p1_world,
    const Point& p2_world) {
    std::vector<json> tracks, vias;
    const auto& layers = grid.layers;
    int n = (int)path.size();
    int i = 0;
    while (i < n) {
        int layer = std::get<0>(path[i]);
        int j = i + 1;
        while (j < n && std::get<0>(path[j]) == layer) ++j;
        std::vector<Point> pts;
        for (int k = i; k < j; ++k) pts.push_back(grid.cell_xy(std::get<1>(path[k]), std::get<2>(path[k])));
        if (i == 0) pts[0] = p1_world;
        if (j >= n) {
            pts[pts.size() - 1] = p2_world;
        } else if (pts.size() == 1) {
            Point via_xy = grid.cell_xy(std::get<1>(path[j]), std::get<2>(path[j]));
            if (pts[0] != via_xy) pts.push_back(via_xy);
        }
        emit_run(pts, layers[layer], net, width, tracks, tcnt);
        if (j < n) {
            Point v = grid.cell_xy(std::get<1>(path[j]), std::get<2>(path[j]));
            vias.push_back(json{{"id", vcnt.next()},
                                {"net", net},
                                {"at", json::array({v.first, v.second})},
                                {"drill", via_drill},
                                {"diameter", via_dia},
                                {"layers", json::array({layers.front(), layers.back()})},
                                {"type", "through"}});
        }
        i = j;
    }
    return {tracks, vias};
}

// Layer indices on which same-net copper covers `at` — the copper layers a route may legally
// start/end on at this terminal (autoroute.py::_terminal_layers). Empty -> caller falls back to all
// layers. A track only connects to a pad on a layer where the pad HAS copper: an F.Cu-only SMD pad
// cannot be reached by a B.Cu track; a tht pad / via spans several layers and allows them all.
std::set<int> terminal_layers(const Grid& grid, const Point& at, const json& net) {
    std::set<int> out;
    for (int li = 0; li < (int)grid.layers.size(); ++li) {
        auto it = grid.shapes_by_layer.find(grid.layers[li]);
        if (it == grid.shapes_by_layer.end()) continue;
        for (const auto& s : it->second) {
            if (same_net(s.net, net) && cell_clear_dist(s, at) <= 1e-9) {
                out.insert(li);
                break;
            }
        }
    }
    return out;
}

// terminal cells for a pad (autoroute.py::_terminal_cells). ``net`` restricts the terminal to the
// layers the pad/via actually occupies; null -> every layer (legacy behaviour).
std::set<Node> terminal_cells(const Grid& grid, const std::vector<std::vector<char>>& blocked,
                              const Point& at, const json& net = json(nullptr)) {
    auto [cx, cy] = grid.nearest_cell(at);
    std::set<Node> out;
    int n_layers = (int)grid.layers.size();
    std::set<int> valid = net.is_null() ? std::set<int>{} : terminal_layers(grid, at, net);
    for (int li = 0; li < n_layers; ++li) {
        if (!valid.empty() && !valid.count(li)) continue;
        bool found = false;
        int fx = 0, fy = 0;
        for (int r = 0; r < 4 && !found; ++r) {
            std::vector<std::pair<int, int>> cands;
            for (int dx = -r; dx <= r; ++dx) {
                for (int dy = -r; dy <= r; ++dy) {
                    if (std::max(std::abs(dx), std::abs(dy)) != r) continue;
                    int ix = cx + dx, iy = cy + dy;
                    if (ix >= 0 && ix < grid.nx && iy >= 0 && iy < grid.ny &&
                        !blocked[li][grid.idx(ix, iy)])
                        cands.push_back({ix, iy});
                }
            }
            if (!cands.empty()) {
                // closest to the true pad point, then index order (c)
                std::stable_sort(cands.begin(), cands.end(),
                                 [&](const std::pair<int, int>& a, const std::pair<int, int>& b) {
                                     double da = dist(grid.cell_xy(a.first, a.second), at);
                                     double db = dist(grid.cell_xy(b.first, b.second), at);
                                     if (da != db) return da < db;
                                     return a < b;
                                 });
                fx = cands[0].first;
                fy = cands[0].second;
                found = true;
            }
        }
        if (found) out.insert(Node{li, fx, fy});
    }
    return out;
}

// per-layer blocked map for a net incl. registry copper + extra obstacles
// (autoroute.py::_blocked_for).
std::vector<std::vector<char>> blocked_for(const Grid& grid, const json& net,
                                           const RoutedRegistry& reg,
                                           const std::vector<Shape>* extra_shapes,
                                           double halo = -1.0) {
    if (halo < 0.0) halo = clearance_of(grid.board, net) + track_width(grid.board, net) / 2.0;
    auto blocked = grid.build_blocked(net, halo);
    std::vector<Shape> obstacles = reg.shapes_for(net.get<std::string>());
    if (extra_shapes) {
        for (const auto& s : *extra_shapes)
            if (!same_net(s.net, net)) obstacles.push_back(s);
    }
    for (size_t li = 0; li < grid.layers.size(); ++li) {
        const std::string& ly = grid.layers[li];
        std::vector<Shape> shp;
        for (const auto& s : obstacles)
            if (s.layers.count(ly)) shp.push_back(s);
        if (!shp.empty()) grid.mark_shapes(blocked[li], shp, halo);
    }
    return blocked;
}

// route one airwire; returns true + fills geom on success (autoroute.py::_route_airwire).
bool route_airwire(const Grid& grid, const json& board, const json& air, const RoutedRegistry& reg,
                   IdCounter& tcnt, IdCounter& vcnt, double via_cost, double bend_cost,
                   std::pair<std::vector<json>, std::vector<json>>& geom,
                   const std::vector<Shape>* extra_shapes = nullptr) {
    json net = air["net"];
    auto blocked = blocked_for(grid, net, reg, extra_shapes);
    Point p1{getf(air["p1"][0]), getf(air["p1"][1])};
    Point p2{getf(air["p2"][0]), getf(air["p2"][1])};
    auto starts = terminal_cells(grid, blocked, p1, net);
    auto goals = terminal_cells(grid, blocked, p2, net);
    if (starts.empty() || goals.empty()) return false;
    // A via needs a larger clearance halo than a track (copper radius via_dia/2 vs track width/2).
    // Build the via-sized blocked map so A* only drops vias where the via copper truly clears
    // other-net copper — otherwise the route is DRC-clean for tracks but the via violates clearance.
    auto [via_dia, via_drill] = via_dims(board, net);
    double via_halo = clearance_of(board, net) + via_dia / 2.0;
    auto via_blocked = blocked_for(grid, net, reg, extra_shapes, via_halo);
    std::vector<Node> path;
    if (!astar(grid, blocked, starts, goals, p2, via_cost, bend_cost, path, &via_blocked))
        return false;
    double width = track_width(board, net);
    geom = path_to_geometry(grid, path, net, width, tcnt, vcnt, via_dia, via_drill, p1, p2);
    return true;
}

// ── Bresenham corridor (autoroute.py::_bresenham) ───────────────────────────────
std::vector<std::pair<int, int>> bresenham(std::pair<int, int> a, std::pair<int, int> b) {
    int x0 = a.first, y0 = a.second, x1 = b.first, y1 = b.second;
    int dx = std::abs(x1 - x0), dy = -std::abs(y1 - y0);
    int sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
    int err = dx + dy;
    std::vector<std::pair<int, int>> out;
    while (true) {
        out.push_back({x0, y0});
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) {
            err += dy;
            x0 += sx;
        }
        if (e2 <= dx) {
            err += dx;
            y0 += sy;
        }
    }
    return out;
}

// blocking nets along the straight corridor (autoroute.py::_blocking_nets).
std::vector<std::string> blocking_nets(const Grid& grid, const json& board, const json& air,
                                       const RoutedRegistry& reg, int budget) {
    json net = air["net"];
    auto p1 = grid.nearest_cell({getf(air["p1"][0]), getf(air["p1"][1])});
    auto p2 = grid.nearest_cell({getf(air["p2"][0]), getf(air["p2"][1])});
    auto corridor = bresenham(p1, p2);
    double clr = clearance_of(board, net);
    double halo = clr + track_width(board, net) / 2.0;
    std::set<std::string> victims;
    for (const auto& [vnet, tv] : reg.tracks) {  // std::map = sorted(key=str)
        if (same_net(json(vnet), net)) continue;
        auto rit = reg.ripup_count.find(vnet);
        if (rit != reg.ripup_count.end() && rit->second >= budget) continue;
        std::vector<Shape> shapes;
        for (const auto& t : tv) {
            Shape sh;
            if (track_shape_of(t, sh)) shapes.push_back(sh);
        }
        auto vit = reg.vias.find(vnet);
        if (vit != reg.vias.end()) {
            for (const auto& v : vit->second) {
                Shape sh;
                via_shape_of(v, sh);
                shapes.push_back(sh);
            }
        }
        for (const auto& [ix, iy] : corridor) {
            Point p = grid.cell_xy(ix, iy);
            bool hit = false;
            for (const auto& s : shapes)
                if (cell_clear_dist(s, p) < halo) {
                    hit = true;
                    break;
                }
            if (hit) {
                victims.insert(vnet);
                break;
            }
        }
    }
    return std::vector<std::string>(victims.begin(), victims.end());  // sorted by str
}

bool geometry_connects(const std::vector<json>& tracks, const std::vector<json>& vias,
                       const json& air) {
    std::vector<Point> pts;
    for (const auto& t : tracks) {
        pts.push_back({getf(t["p1"][0]), getf(t["p1"][1])});
        pts.push_back({getf(t["p2"][0]), getf(t["p2"][1])});
    }
    for (const auto& v : vias) pts.push_back({getf(v["at"][0]), getf(v["at"][1])});
    if (pts.empty()) return false;
    Point p1{getf(air["p1"][0]), getf(air["p1"][1])};
    Point p2{getf(air["p2"][0]), getf(air["p2"][1])};
    bool near1 = false, near2 = false;
    for (const auto& p : pts) {
        if (dist(p, p1) <= 1e-6) near1 = true;
        if (dist(p, p2) <= 1e-6) near2 = true;
    }
    return near1 && near2;
}

json airwire_from_geometry(const std::string& net, const std::vector<json>& tracks,
                           const std::vector<json>& vias) {
    std::vector<Point> pts;
    for (const auto& t : tracks) {
        pts.push_back({getf(t["p1"][0]), getf(t["p1"][1])});
        pts.push_back({getf(t["p2"][0]), getf(t["p2"][1])});
    }
    if (pts.empty()) {
        for (const auto& v : vias) pts.push_back({getf(v["at"][0]), getf(v["at"][1])});
    }
    if (pts.size() < 2) {
        Point p = pts.empty() ? Point{0.0, 0.0} : pts[0];
        return json{{"net", net},
                    {"p1", json::array({p.first, p.second})},
                    {"p2", json::array({p.first, p.second})}};
    }
    double best = -1.0;
    Point a = pts[0], b = pts[0];
    for (size_t i = 0; i < pts.size(); ++i)
        for (size_t j = i + 1; j < pts.size(); ++j) {
            double d = dist(pts[i], pts[j]);
            if (d > best) {
                best = d;
                a = pts[i];
                b = pts[j];
            }
        }
    if (std::make_pair(a.first, a.second) > std::make_pair(b.first, b.second)) std::swap(a, b);
    return json{{"net", net},
                {"p1", json::array({a.first, a.second})},
                {"p2", json::array({b.first, b.second})}};
}

void restore(RoutedRegistry& reg,
             const std::map<std::string, std::pair<std::vector<json>, std::vector<json>>>& snap) {
    for (const auto& [net, tv] : snap) {
        reg.tracks[net] = tv.first;
        reg.vias[net] = tv.second;
    }
}

// rescue one failed airwire by rip-up (autoroute.py::_try_rescue).
bool try_rescue(const Grid& grid, const json& board, const json& air,
                const std::map<std::string, std::vector<json>>& airwire_map, RoutedRegistry& reg,
                IdCounter& tcnt, IdCounter& vcnt, double via_cost, double bend_cost, int budget) {
    json net = air["net"];
    auto victims = blocking_nets(grid, board, air, reg, budget);
    if (victims.empty()) {
        std::pair<std::vector<json>, std::vector<json>> geom;
        if (route_airwire(grid, board, air, reg, tcnt, vcnt, via_cost, bend_cost, geom)) {
            reg.add(net.get<std::string>(), geom.first, geom.second);
            return true;
        }
        return false;
    }

    std::map<std::string, std::pair<std::vector<json>, std::vector<json>>> snapshot;
    std::vector<json> victim_airwires;
    for (const auto& v : victims) {
        auto [t, vi] = reg.pop(v);
        snapshot[v] = {t, vi};
        auto it = airwire_map.find(v);
        if (it != airwire_map.end() && !it->second.empty()) {
            for (const auto& w : it->second) victim_airwires.push_back(w);
        } else {
            victim_airwires.push_back(airwire_from_geometry(v, t, vi));
        }
    }

    std::pair<std::vector<json>, std::vector<json>> geom;
    if (!route_airwire(grid, board, air, reg, tcnt, vcnt, via_cost, bend_cost, geom)) {
        restore(reg, snapshot);
        return false;
    }
    reg.add(net.get<std::string>(), geom.first, geom.second);

    // re-route victims, shortest corridor first (deterministic).
    std::stable_sort(victim_airwires.begin(), victim_airwires.end(), [](const json& a, const json& b) {
        double la = std::hypot(getf(a["p1"][0]) - getf(a["p2"][0]), getf(a["p1"][1]) - getf(a["p2"][1]));
        double lb = std::hypot(getf(b["p1"][0]) - getf(b["p2"][0]), getf(b["p1"][1]) - getf(b["p2"][1]));
        if (la != lb) return la < lb;
        std::string na = a["net"].get<std::string>(), nb = b["net"].get<std::string>();
        if (na != nb) return na < nb;
        std::vector<double> ap1{getf(a["p1"][0]), getf(a["p1"][1])};
        std::vector<double> bp1{getf(b["p1"][0]), getf(b["p1"][1])};
        if (ap1 != bp1) return ap1 < bp1;
        std::vector<double> ap2{getf(a["p2"][0]), getf(a["p2"][1])};
        std::vector<double> bp2{getf(b["p2"][0]), getf(b["p2"][1])};
        return ap2 < bp2;
    });

    std::vector<std::string> placed{net.get<std::string>()};
    bool ok = true;
    for (const auto& rv : victim_airwires) {
        std::pair<std::vector<json>, std::vector<json>> g;
        if (!route_airwire(grid, board, rv, reg, tcnt, vcnt, via_cost, bend_cost, g) ||
            !geometry_connects(g.first, g.second, rv)) {
            ok = false;
            break;
        }
        reg.add(rv["net"].get<std::string>(), g.first, g.second);
        placed.push_back(rv["net"].get<std::string>());
    }
    if (!ok) {
        for (const auto& r : placed) {
            reg.tracks.erase(r);
            reg.vias.erase(r);
        }
        restore(reg, snapshot);
        return false;
    }
    return true;
}

// bounded rip-up-and-reroute (autoroute.py::_ripup_reroute).
int ripup_reroute(const Grid& grid, const json& board, std::vector<json> failed,
                  const std::map<std::string, std::vector<json>>& airwire_map, RoutedRegistry& reg,
                  IdCounter& tcnt, IdCounter& vcnt, double via_cost, double bend_cost, int passes,
                  int budget) {
    int iterations = 0;
    std::vector<json> pending = failed;
    for (int p = 0; p < passes; ++p) {
        if (pending.empty()) break;
        std::vector<json> still;
        std::stable_sort(pending.begin(), pending.end(), [](const json& a, const json& b) {
            double la = std::hypot(getf(a["p1"][0]) - getf(a["p2"][0]),
                                   getf(a["p1"][1]) - getf(a["p2"][1]));
            double lb = std::hypot(getf(b["p1"][0]) - getf(b["p2"][0]),
                                   getf(b["p1"][1]) - getf(b["p2"][1]));
            if (-la != -lb) return -la < -lb;  // longest corridor first
            std::string na = a["net"].get<std::string>(), nb = b["net"].get<std::string>();
            if (na != nb) return na < nb;
            std::vector<double> ap1{getf(a["p1"][0]), getf(a["p1"][1])};
            std::vector<double> bp1{getf(b["p1"][0]), getf(b["p1"][1])};
            if (ap1 != bp1) return ap1 < bp1;
            std::vector<double> ap2{getf(a["p2"][0]), getf(a["p2"][1])};
            std::vector<double> bp2{getf(b["p2"][0]), getf(b["p2"][1])};
            return ap2 < bp2;
        });
        for (const auto& air : pending) {
            iterations += 1;
            if (try_rescue(grid, board, air, airwire_map, reg, tcnt, vcnt, via_cost, bend_cost, budget))
                continue;
            still.push_back(air);
        }
        if (still.size() == pending.size()) break;
        pending = still;
    }
    return iterations;
}

// ── differential pairs ──────────────────────────────────────────────────────────
const char* DIFF_SUF[3][2] = {{"_P", "_N"}, {"_p", "_n"}, {"+", "-"}};

bool ends_with(const std::string& s, const std::string& suf) {
    return s.size() >= suf.size() && s.compare(s.size() - suf.size(), suf.size(), suf) == 0;
}

// returns {found, partner, is_positive}.
std::tuple<bool, std::string, bool> diff_complement(const std::string& name) {
    for (int i = 0; i < 3; ++i) {
        std::string pos = DIFF_SUF[i][0], neg = DIFF_SUF[i][1];
        if (ends_with(name, pos)) return {true, name.substr(0, name.size() - pos.size()) + neg, true};
        if (ends_with(name, neg)) return {true, name.substr(0, name.size() - neg.size()) + pos, false};
    }
    return {false, "", false};
}

std::vector<std::pair<std::string, std::string>> resolve_diff_pairs(const json& /*board*/,
                                                                    const json& airwires,
                                                                    const json& opts) {
    std::set<std::string> have;
    for (const auto& a : airwires) have.insert(a["net"].get<std::string>());
    std::vector<std::pair<std::string, std::string>> pairs;
    std::set<std::string> seen;

    if (opts.contains("diff_pairs") && opts["diff_pairs"].is_array() &&
        !opts["diff_pairs"].empty()) {
        for (const auto& entry : opts["diff_pairs"]) {
            if (!entry.is_array() || entry.size() < 2) continue;
            std::string a = entry[0].is_string() ? entry[0].get<std::string>() : entry[0].dump();
            std::string b = entry[1].is_string() ? entry[1].get<std::string>() : entry[1].dump();
            if (seen.count(a) || seen.count(b) || !have.count(a) || !have.count(b)) continue;
            auto [found, partner, is_pos] = diff_complement(a);
            if (found && !is_pos) std::swap(a, b);
            pairs.push_back({a, b});
            seen.insert(a);
            seen.insert(b);
        }
        return pairs;
    }
    if (opts.value("no_auto_diff", false)) return pairs;

    for (const auto& net : have) {  // sorted(have): std::set iterates sorted
        if (seen.count(net)) continue;
        auto [found, partner, is_pos] = diff_complement(net);
        if (!found) continue;
        if (!have.count(partner) || seen.count(partner)) continue;
        std::pair<std::string, std::string> pair = is_pos ? std::make_pair(net, partner)
                                                          : std::make_pair(partner, net);
        pairs.push_back(pair);
        seen.insert(pair.first);
        seen.insert(pair.second);
    }
    return pairs;
}

// order a net's segment tracks into a single open polyline (autoroute.py::_polyline_of).
std::vector<Point> polyline_of(const std::vector<json>& tracks) {
    std::vector<std::pair<Point, Point>> segs;
    for (const auto& t : tracks) {
        if (t.value("kind", std::string("segment")) != "segment") continue;
        segs.push_back({{getf(t["p1"][0]), getf(t["p1"][1])}, {getf(t["p2"][0]), getf(t["p2"][1])}});
    }
    if (segs.empty()) return {};
    auto key = [](const Point& p) -> std::pair<double, double> {
        return {py_round_n(p.first, 4), py_round_n(p.second, 4)};
    };
    std::map<std::pair<double, double>, std::vector<std::pair<std::pair<double, double>, Point>>> adj;
    for (const auto& [a, b] : segs) {
        adj[key(a)].push_back({key(b), b});
        adj[key(b)].push_back({key(a), a});
    }
    std::vector<std::pair<double, double>> ends;
    for (const auto& [k, v] : adj)
        if (v.size() == 1) ends.push_back(k);
    if (ends.empty()) return {};
    std::pair<double, double> start = *std::min_element(ends.begin(), ends.end());
    bool found_start = false;
    Point start_xy{0, 0};
    for (const auto& [a, b] : segs) {
        if (key(a) == start) {
            start_xy = a;
            found_start = true;
            break;
        }
        if (key(b) == start) {
            start_xy = b;
            found_start = true;
            break;
        }
    }
    if (!found_start) return {};
    std::vector<Point> poly{start_xy};
    std::set<std::pair<std::pair<double, double>, std::pair<double, double>>> visited;
    std::pair<double, double> cur = start;
    int steps = 0;
    while (steps <= (int)segs.size() + 1) {
        steps += 1;
        bool have_next = false;
        std::pair<double, double> nk;
        Point nxy{0, 0};
        for (const auto& [k2, xy] : adj[cur]) {
            auto edge = std::minmax(cur, k2);
            std::pair<std::pair<double, double>, std::pair<double, double>> e{edge.first, edge.second};
            if (visited.count(e)) continue;
            nk = k2;
            nxy = xy;
            have_next = true;
            visited.insert(e);
            break;
        }
        if (!have_next) break;
        poly.push_back(nxy);
        cur = nk;
    }
    return poly;
}

double polyline_length(const std::vector<Point>& poly) {
    double s = 0.0;
    for (size_t i = 0; i + 1 < poly.size(); ++i) s += dist(poly[i], poly[i + 1]);
    return s;
}

std::vector<json> polyline_to_tracks(const std::vector<Point>& poly, const json& net, double width,
                                     const std::string& layer, IdCounter& tcnt) {
    std::vector<json> tracks;
    emit_run(poly, layer, net, width, tracks, tcnt);
    return tracks;
}

// True if poly stays clear of OTHER-net copper + inside outline (autoroute.py::_poly_clear_of_others).
bool poly_clear_of_others(const Grid& grid, const json& board, const std::vector<Point>& poly,
                          const std::string& layer, const json& net, const RoutedRegistry& reg,
                          const json& partner) {
    double clr = clearance_of(board, net);
    double halo = clr + track_width(board, net) / 2.0;
    std::vector<Shape> others;
    std::vector<Shape> src = copper_shapes(board);
    std::vector<Shape> reg_sh = reg.shapes_for(net.get<std::string>());
    src.insert(src.end(), reg_sh.begin(), reg_sh.end());
    for (const auto& s : src) {
        if (s.layers.count(layer) && !same_net(s.net, net) && !same_net(s.net, partner))
            others.push_back(s);
    }
    std::vector<Point> samples(poly);
    for (size_t i = 0; i + 1 < poly.size(); ++i)
        samples.push_back({(poly[i].first + poly[i + 1].first) / 2.0,
                           (poly[i].second + poly[i + 1].second) / 2.0});
    for (const auto& p : samples) {
        auto [ix, iy] = grid.nearest_cell(p);
        if (!grid.outline_mask[grid.idx(ix, iy)]) {
            if (!poly.empty() && p != poly.front() && p != poly.back()) return false;
        }
        for (const auto& s : others)
            if (cell_clear_dist(s, p) < halo - 1e-9) return false;
    }
    return true;
}

// build complement polyline by offsetting primary's dominant run (autoroute.py::_couple_complement).
// returns {ok, poly, side}.
std::tuple<bool, std::vector<Point>, int> couple_complement(const std::vector<Point>& poly_p,
                                                            const json& air_n, double offset) {
    int best_i = -1;
    double best_len = 0.0;
    for (size_t i = 0; i + 1 < poly_p.size(); ++i) {
        double d = dist(poly_p[i], poly_p[i + 1]);
        if (d > best_len) {
            best_len = d;
            best_i = (int)i;
        }
    }
    if (best_i < 0 || best_len <= 1e-6) return {false, {}, 0};
    Point a = poly_p[best_i], b = poly_p[best_i + 1];
    double dx = b.first - a.first, dy = b.second - a.second;
    double ln = std::hypot(dx, dy);
    double lnx = -dy / ln, lny = dx / ln;
    Point np1{getf(air_n["p1"][0]), getf(air_n["p1"][1])};
    Point np2{getf(air_n["p2"][0]), getf(air_n["p2"][1])};
    Point mid{(a.first + b.first) / 2.0, (a.second + b.second) / 2.0};
    Point npmid{(np1.first + np2.first) / 2.0, (np1.second + np2.second) / 2.0};
    double dot = (npmid.first - mid.first) * lnx + (npmid.second - mid.second) * lny;
    int side = dot >= 0 ? 1 : -1;
    Point oa{a.first + side * offset * lnx, a.second + side * offset * lny};
    Point ob{b.first + side * offset * lnx, b.second + side * offset * lny};
    std::vector<Point> run;
    if (dist(np1, oa) + dist(np2, ob) <= dist(np1, ob) + dist(np2, oa))
        run = {np1, oa, ob, np2};
    else
        run = {np1, ob, oa, np2};
    std::vector<Point> poly{run[0]};
    for (size_t i = 1; i < run.size(); ++i)
        if (dist(run[i], poly.back()) > 1e-9) poly.push_back(run[i]);
    if (poly.size() < 2) return {false, {}, 0};
    return {true, poly, side};
}

double coupled_pct(const std::vector<Point>& poly_p, const std::vector<Point>& poly_n,
                   double offset) {
    if (poly_p.size() < 2 || poly_n.size() < 2) return 0.0;
    double tol = offset * 0.6 + 1e-6;
    double total = 0.0, coupled = 0.0;
    for (size_t i = 0; i + 1 < poly_p.size(); ++i) {
        Point a = poly_p[i], b = poly_p[i + 1];
        double seg = dist(a, b);
        if (seg <= 1e-9) continue;
        total += seg;
        Point mid{(a.first + b.first) / 2.0, (a.second + b.second) / 2.0};
        double dmin = std::numeric_limits<double>::infinity();
        for (size_t j = 0; j + 1 < poly_n.size(); ++j)
            dmin = std::min(dmin, point_seg_dist(mid, poly_n[j], poly_n[j + 1]));
        if (std::abs(dmin - offset) <= tol) coupled += seg;
    }
    return total > 0 ? 100.0 * coupled / total : 0.0;
}

bool crosses_partner(const std::vector<Point>& poly, const std::vector<json>& partner_tracks) {
    for (size_t i = 0; i + 1 < poly.size(); ++i) {
        Point a = poly[i], b = poly[i + 1];
        for (const auto& t : partner_tracks) {
            if (t.value("kind", std::string("segment")) != "segment") continue;
            Point c{getf(t["p1"][0]), getf(t["p1"][1])};
            Point d{getf(t["p2"][0]), getf(t["p2"][1])};
            if (seg_seg_dist(a, b, c, d) <= 1e-9) return true;
        }
    }
    return false;
}

// rectangular accordion meander (autoroute.py::_meander). returns {ok, poly}.
std::pair<bool, std::vector<Point>> meander(const std::vector<Point>& poly, double extra,
                                            double /*gap*/, double width, int side) {
    if (extra <= 0 || poly.size() < 2) return {true, poly};
    int best_i = -1;
    double best_len = 0.0;
    for (size_t i = 0; i + 1 < poly.size(); ++i) {
        double d = dist(poly[i], poly[i + 1]);
        if (d > best_len) {
            best_len = d;
            best_i = (int)i;
        }
    }
    double min_amp = std::max(width, 0.2);
    if (best_i < 0 || best_len < 4.0 * min_amp) return {false, {}};
    Point a = poly[best_i], b = poly[best_i + 1];
    double dx = b.first - a.first, dy = b.second - a.second;
    double seg_len = std::hypot(dx, dy);
    double ux = dx / seg_len, uy = dy / seg_len;
    double sgn = side >= 0 ? 1.0 : -1.0;
    double nx = -uy * sgn, ny = ux * sgn;
    double usable = seg_len * 0.9;
    int teeth = std::max(1, (int)std::floor(extra / (2.0 * min_amp)));
    teeth = std::min(teeth, MAX_MEANDER_TEETH);
    double amp = extra / (2.0 * teeth);
    double tp = 2.0 * amp;
    double span = teeth * tp;
    if (span > usable) return {false, {}};
    double start_off = (seg_len - span) / 2.0;
    std::vector<Point> mid{a};
    auto at = [&](double off, double lift) -> Point {
        return {a.first + ux * off + nx * lift, a.second + uy * off + ny * lift};
    };
    double pos = start_off;
    mid.push_back(at(pos, 0.0));
    double half = tp / 2.0;
    for (int k = 0; k < teeth; ++k) {
        mid.push_back(at(pos, amp));
        mid.push_back(at(pos + half, amp));
        mid.push_back(at(pos + half, 0.0));
        mid.push_back(at(pos + tp, 0.0));
        pos += tp;
    }
    mid.push_back(b);
    std::vector<Point> out;
    for (int i = 0; i < best_i; ++i) out.push_back(poly[i]);
    out.insert(out.end(), mid.begin(), mid.end());
    for (size_t i = best_i + 2; i < poly.size(); ++i) out.push_back(poly[i]);
    return {true, out};
}

std::pair<bool, std::vector<Point>> meander_away(const Grid& grid, const json& board,
                                                 const std::vector<Point>& poly, double extra,
                                                 double gap, double width, const std::string& layer,
                                                 const json& net, const json& partner,
                                                 const RoutedRegistry& reg, int away) {
    std::vector<int> sides;
    if (away == 1 || away == -1)
        sides = {away};
    else
        sides = {1, -1};
    for (int s : sides) {
        auto [ok, cand] = meander(poly, extra, gap, width, s);
        if (!ok) continue;
        if (!poly_clear_of_others(grid, board, cand, layer, net, reg, partner)) continue;
        auto pit = reg.tracks.find(partner.get<std::string>());
        std::vector<json> ptracks = pit != reg.tracks.end() ? pit->second : std::vector<json>();
        if (crosses_partner(cand, ptracks)) continue;
        return {true, cand};
    }
    return {false, {}};
}

double round4(double v) { return py_round_n(v, 4); }

// route the pair + register both members; returns the per-pair stats dict
// (autoroute.py::_route_diff_pair_into).
json route_diff_pair_into(const Grid& grid, const json& board, const std::string& net_p,
                          const std::string& net_n, RoutedRegistry& reg, IdCounter& tcnt,
                          IdCounter& vcnt, double via_cost, double bend_cost, const json& opts) {
    json stats = {{"nets", json::array({net_p, net_n})}, {"routed", false},
                  {"length_p", 0.0},                      {"length_n", 0.0},
                  {"skew_mm", 0.0},                       {"coupled_pct", 0.0}};

    json rats = ratsnest(board);
    const json* air_p = nullptr;
    const json* air_n = nullptr;
    for (const auto& a : rats) {
        if (!air_p && a["net"].get<std::string>() == net_p) air_p = &a;
        if (!air_n && a["net"].get<std::string>() == net_n) air_n = &a;
    }
    if (!air_p) return stats;

    json override_gap = opts.contains("diff_gap") ? opts["diff_gap"] : json(nullptr);
    double gap = diff_gap_of(board, json(net_p), override_gap);
    double wp = track_width(board, json(net_p));
    double wn = track_width(board, json(net_n));
    double offset = gap + wp / 2.0 + wn / 2.0;
    double length_tol = opts.contains("length_tol") ? getf(opts["length_tol"]) : DEFAULT_LENGTH_TOL;

    std::pair<std::vector<json>, std::vector<json>> gp;
    if (!route_airwire(grid, board, *air_p, reg, tcnt, vcnt, via_cost, bend_cost, gp)) return stats;
    reg.add(net_p, gp.first, gp.second);

    std::vector<Point> poly_p = polyline_of(gp.first);
    std::string layer = !gp.first.empty() ? gp.first[0]["layer"].get<std::string>() : grid.layers[0];

    bool coupled = false;
    int coupled_side = 0;
    bool have_gn = false;
    std::pair<std::vector<json>, std::vector<json>> gn;
    if (!poly_p.empty() && air_n && gp.second.empty()) {
        auto [ok, cand, side] = couple_complement(poly_p, *air_n, offset);
        coupled_side = side;
        if (ok && poly_clear_of_others(grid, board, cand, layer, json(net_n), reg, json(net_p))) {
            auto tn = polyline_to_tracks(cand, json(net_n), wn, layer, tcnt);
            reg.add(net_n, tn, {});
            gn = {tn, {}};
            have_gn = true;
            coupled = true;
        }
    }

    if (!have_gn && air_n) {
        if (route_airwire(grid, board, *air_n, reg, tcnt, vcnt, via_cost, bend_cost, gn)) {
            reg.add(net_n, gn.first, gn.second);
            have_gn = true;
        }
    }

    if (!have_gn) {
        stats["length_p"] = round4(polyline_length(poly_p));
        return stats;
    }

    poly_p = polyline_of(reg.tracks.count(net_p) ? reg.tracks[net_p] : std::vector<json>());
    std::vector<Point> poly_n =
        polyline_of(reg.tracks.count(net_n) ? reg.tracks[net_n] : std::vector<json>());
    double len_p = polyline_length(poly_p);
    double len_n = polyline_length(poly_n);
    double skew = std::abs(len_p - len_n);
    if (skew > length_tol) {
        std::string short_net;
        std::vector<Point> short_poly;
        double short_w;
        json short_partner;
        int away;
        if (len_p < len_n) {
            short_net = net_p;
            short_poly = poly_p;
            short_w = wp;
            short_partner = net_n;
            away = coupled_side ? -coupled_side : 0;
        } else {
            short_net = net_n;
            short_poly = poly_n;
            short_w = wn;
            short_partner = net_p;
            away = coupled_side ? coupled_side : 0;
        }
        auto [ok, new_poly] = meander_away(grid, board, short_poly, skew, gap, short_w, layer,
                                           json(short_net), short_partner, reg, away);
        if (ok) {
            reg.tracks[short_net] = polyline_to_tracks(new_poly, json(short_net), short_w, layer, tcnt);
            if (short_net == net_p) {
                poly_p = polyline_of(reg.tracks[net_p]);
                len_p = polyline_length(poly_p);
            } else {
                poly_n = polyline_of(reg.tracks[net_n]);
                len_n = polyline_length(poly_n);
            }
        }
        skew = std::abs(len_p - len_n);
    }

    stats["routed"] = true;
    stats["length_p"] = round4(len_p);
    stats["length_n"] = round4(len_n);
    stats["skew_mm"] = round4(skew);
    stats["coupled_pct"] = coupled ? py_round_n(coupled_pct(poly_p, poly_n, offset), 1) : 0.0;
    return stats;
}

}  // namespace

// ── public API ──────────────────────────────────────────────────────────────────
json autoroute(const json& board, const json& opts_in) {
    json opts = opts_in.is_object() ? opts_in : json::object();
    json report = {
        {"routed", json::array()},
        {"failed", json::array()},
        {"length_mm", 0.0},
        {"airwires_before", 0},
        {"airwires_after", 0},
        {"vias", 0},
        {"grid", json::object()},
        {"ripup_iterations", 0},
        {"diff_pairs", json::array()},
        {"note",
         "basic grid (Lee/A*) router: orthogonal tracks + through-vias, best-effort, "
         "with bounded rip-up-and-reroute + parallel-offset differential pairs; "
         "no push-and-shove / negotiated-congestion; failed nets are reported"},
    };
    json out = {{"tracks", json::array()}, {"vias", json::array()}, {"report", report}};

    if (!schema::is_board(board)) return out;

    json airwires = ratsnest(board);
    out["report"]["airwires_before"] = (int)airwires.size();
    if (airwires.empty()) {
        double pitch0 = opts.contains("pitch") ? getf(opts["pitch"]) : DEFAULT_PITCH;
        out["report"]["grid"] = {{"pitch", pitch0}, {"nx", 0}, {"ny", 0}};
        return out;
    }

    double pitch = opts.contains("pitch") ? getf(opts["pitch"]) : DEFAULT_PITCH;
    long max_cells = opts.contains("max_grid_cells") ? (long)getf(opts["max_grid_cells"])
                                                      : MAX_GRID_CELLS;
    auto [minx, miny, maxx, maxy] = board_bbox(board);
    double w = maxx - minx, hgt = maxy - miny;
    while (pitch > 0 && ((long)((long)(w / pitch) + 1) * ((long)(hgt / pitch) + 1)) > max_cells)
        pitch *= 1.5;
    Grid grid(board, pitch);
    out["report"]["grid"] = {{"pitch", round4(pitch)},
                             {"nx", grid.nx},
                             {"ny", grid.ny},
                             {"layers", (int)grid.layers.size()}};

    double via_cost = opts.contains("via_cost") ? getf(opts["via_cost"]) : VIA_COST;
    double bend_cost = opts.contains("bend_cost") ? getf(opts["bend_cost"]) : BEND_COST;
    int ripup_passes = std::max(0, opts.contains("ripup_passes") ? (int)getf(opts["ripup_passes"])
                                                                 : DEFAULT_RIPUP_PASSES);
    int ripup_budget = std::max(0, opts.contains("ripup_budget") ? (int)getf(opts["ripup_budget"])
                                                                 : DEFAULT_RIPUP_BUDGET);

    IdCounter tcnt(board, "t", "tracks");
    IdCounter vcnt(board, "v", "vias");
    RoutedRegistry reg;

    // 1) differential pairs first
    auto diff_pairs = resolve_diff_pairs(board, airwires, opts);
    std::set<std::string> diff_nets;
    for (const auto& [np_net, nn_net] : diff_pairs) {
        diff_nets.insert(np_net);
        diff_nets.insert(nn_net);
        json stats =
            route_diff_pair_into(grid, board, np_net, nn_net, reg, tcnt, vcnt, via_cost, bend_cost, opts);
        out["report"]["diff_pairs"].push_back(stats);
    }

    // 2) greedy single pass over remaining airwires, shortest-first
    std::vector<json> single_airwires;
    for (const auto& a : airwires)
        if (!diff_nets.count(a["net"].get<std::string>())) single_airwires.push_back(a);
    std::vector<json> ordered = single_airwires;
    std::stable_sort(ordered.begin(), ordered.end(), [](const json& a, const json& b) {
        double la = std::hypot(getf(a["p1"][0]) - getf(a["p2"][0]), getf(a["p1"][1]) - getf(a["p2"][1]));
        double lb = std::hypot(getf(b["p1"][0]) - getf(b["p2"][0]), getf(b["p1"][1]) - getf(b["p2"][1]));
        if (la != lb) return la < lb;
        std::string na = a["net"].get<std::string>(), nb = b["net"].get<std::string>();
        if (na != nb) return na < nb;
        std::vector<double> ap1{getf(a["p1"][0]), getf(a["p1"][1])};
        std::vector<double> bp1{getf(b["p1"][0]), getf(b["p1"][1])};
        if (ap1 != bp1) return ap1 < bp1;
        std::vector<double> ap2{getf(a["p2"][0]), getf(a["p2"][1])};
        std::vector<double> bp2{getf(b["p2"][0]), getf(b["p2"][1])};
        return ap2 < bp2;
    });

    std::vector<json> failed_airwires;
    for (const auto& air : ordered) {
        std::pair<std::vector<json>, std::vector<json>> geom;
        if (!route_airwire(grid, board, air, reg, tcnt, vcnt, via_cost, bend_cost, geom)) {
            failed_airwires.push_back(air);
            continue;
        }
        reg.add(air["net"].get<std::string>(), geom.first, geom.second);
    }

    // 3) bounded rip-up-and-reroute
    if (ripup_passes > 0 && !failed_airwires.empty()) {
        std::map<std::string, std::vector<json>> airwire_map;
        for (const auto& a : single_airwires) airwire_map[a["net"].get<std::string>()].push_back(a);
        out["report"]["ripup_iterations"] =
            ripup_reroute(grid, board, failed_airwires, airwire_map, reg, tcnt, vcnt, via_cost,
                          bend_cost, ripup_passes, ripup_budget);
    }

    std::vector<json> out_tracks = reg.all_tracks();
    std::vector<json> out_vias = reg.all_vias();

    // routed board (copy) to measure the post-route ratsnest honestly
    json routed_board = board;
    json tlist = routed_board.contains("tracks") ? routed_board["tracks"] : json::array();
    for (const auto& t : out_tracks) tlist.push_back(t);
    routed_board["tracks"] = tlist;
    json vlist = routed_board.contains("vias") ? routed_board["vias"] : json::array();
    for (const auto& v : out_vias) vlist.push_back(v);
    routed_board["vias"] = vlist;
    json after = ratsnest(routed_board);
    out["report"]["airwires_after"] = (int)after.size();

    // Python: sum(<generator>) over an EMPTY track list returns the int 0, and
    // round(0, 4) stays int 0; with tracks it is a float. Mirror that type exactly
    // so json.dumps matches (0 vs 0.0).
    if (out_tracks.empty()) {
        out["report"]["length_mm"] = 0;  // integer 0
    } else {
        double length = 0.0;
        for (const auto& t : out_tracks)
            length += std::hypot(getf(t["p2"][0]) - getf(t["p1"][0]),
                                 getf(t["p2"][1]) - getf(t["p1"][1]));
        out["report"]["length_mm"] = round4(length);
    }
    out["report"]["vias"] = (int)out_vias.size();

    std::set<std::string> after_nets;
    for (const auto& a : after) after_nets.insert(a["net"].get<std::string>());
    std::set<std::string> touched;
    for (const auto& a : airwires) touched.insert(a["net"].get<std::string>());
    std::vector<std::string> routed_nets;
    for (const auto& n : touched)
        if (!after_nets.count(n)) routed_nets.push_back(n);
    std::sort(routed_nets.begin(), routed_nets.end());
    out["report"]["routed"] = routed_nets;
    std::vector<std::string> failed_nets(after_nets.begin(), after_nets.end());
    std::sort(failed_nets.begin(), failed_nets.end());
    out["report"]["failed"] = failed_nets;

    out["tracks"] = out_tracks;
    out["vias"] = out_vias;
    return out;
}

json route_diff_pair(const json& board, const std::string& net_p, const std::string& net_n,
                     const json& opts_in) {
    json opts = opts_in.is_object() ? opts_in : json::object();
    json out = {{"tracks", json::array()}, {"vias", json::array()}, {"report", json::object()}};
    if (!schema::is_board(board)) {
        out["report"] = {{"nets", json::array({net_p, net_n})}, {"routed", false},
                         {"length_p", 0.0},                      {"length_n", 0.0},
                         {"skew_mm", 0.0},                       {"coupled_pct", 0.0}};
        return out;
    }
    double pitch = opts.contains("pitch") ? getf(opts["pitch"]) : DEFAULT_PITCH;
    long max_cells = opts.contains("max_grid_cells") ? (long)getf(opts["max_grid_cells"])
                                                      : MAX_GRID_CELLS;
    auto [minx, miny, maxx, maxy] = board_bbox(board);
    double w = maxx - minx, hgt = maxy - miny;
    while (pitch > 0 && ((long)((long)(w / pitch) + 1) * ((long)(hgt / pitch) + 1)) > max_cells)
        pitch *= 1.5;
    Grid grid(board, pitch);
    double via_cost = opts.contains("via_cost") ? getf(opts["via_cost"]) : VIA_COST;
    double bend_cost = opts.contains("bend_cost") ? getf(opts["bend_cost"]) : BEND_COST;
    IdCounter tcnt(board, "t", "tracks");
    IdCounter vcnt(board, "v", "vias");
    RoutedRegistry reg;
    json stats =
        route_diff_pair_into(grid, board, net_p, net_n, reg, tcnt, vcnt, via_cost, bend_cost, opts);
    out["tracks"] = reg.all_tracks();
    out["vias"] = reg.all_vias();
    out["report"] = stats;
    return out;
}

}  // namespace aicad::ecad
