// DRC + ratsnest implementation — see drc.h. OCCT-free, pure JSON/geometry:
// normalized copper shapes (including conservative arc capsules), filled-zone
// polygon checks, the rule catalog, ratsnest MST, and deterministic ordering.

#include "ecad/drc.h"

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

#include "ecad/schema.h"

namespace chudware::ecad {

using nlohmann::json;

namespace {

constexpr double kEps = 1e-9;
constexpr double kTouch = 1e-9;
constexpr double kPi = 3.14159265358979323846;

using Point = std::pair<double, double>;

double getf(const json& v, double dflt = 0.0) {
    return v.is_number() ? v.get<double>() : dflt;
}

// Python's format(x, ".4g"). C printf("%.4g") matches for all values here.
std::string fmt4g(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.4g", v);
    return std::string(buf);
}

// Python repr() of a net value: a JSON string -> 'name' (single-quoted); null -> None.
std::string repr_net(const json& net) {
    if (net.is_string()) return "'" + net.get<std::string>() + "'";
    if (net.is_null()) return "None";
    return net.dump();
}

double clamp01(double t) { return t < 0.0 ? 0.0 : (t > 1.0 ? 1.0 : t); }

double dist(const Point& a, const Point& b) {
    return std::hypot(a.first - b.first, a.second - b.second);
}

int arc_segment_count(double a0, double a1) {
    return std::max(8, static_cast<int>(std::abs(a1 - a0) / (kPi / 18.0)));
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

bool shared_layer(const Shape& a, const Shape& b) {
    for (const auto& l : a.layers)
        if (b.layers.count(l)) return true;
    return false;
}

double edge_distance(const Shape& a, const Shape& b);  // fwd

// Edge-to-edge copper distance between two normalized shapes — the TRUE SIGNED gap
// (positive = clear, 0 = touching, negative = copper overlap). Mirrors
// engine/ecad/drc.py::edge_distance EXACTLY. The DRC clearance reporter must surface the
// real measurement, so this MUST NOT clamp at 0 — clamping made overlapping/near-touching
// copper (e.g. tight SOIC pads) read as "clearance 0mm" instead of the actual gap.
// Callers that need a non-negative value clamp themselves (see shape_edge_dist).
double edge_distance(const Shape& a, const Shape& b) {
    if (a.kind == "seg" && b.kind == "seg") {
        double d = seg_seg_dist(a.p1, a.p2, b.p1, b.p2);
        return d - a.margin - b.margin;
    }
    if (a.kind == "seg" && b.kind == "circle") {
        double d = point_seg_dist(b.center, a.p1, a.p2);
        return d - a.margin - b.margin;
    }
    if (a.kind == "circle" && b.kind == "seg") return edge_distance(b, a);
    if (a.kind == "circle" && b.kind == "circle")
        return dist(a.center, b.center) - a.margin - b.margin;
    if (a.kind == "seg" && b.kind == "box") {
        double d = seg_box_dist(a.p1, a.p2, b.center.first, b.center.second, b.half.first,
                                b.half.second);
        return d - a.margin;
    }
    if (a.kind == "box" && b.kind == "seg") return edge_distance(b, a);
    if (a.kind == "circle" && b.kind == "box") {
        double d = point_box_dist(a.center, b.center.first, b.center.second, b.half.first,
                                  b.half.second);
        return d - a.margin;
    }
    if (a.kind == "box" && b.kind == "circle") return edge_distance(b, a);
    // box / box — reconstruct a signed value so overlapping boxes report a negative gap
    // (matches drc.py: penetration depth along the shallowest axis when overlapping).
    double dx = std::abs(a.center.first - b.center.first) - (a.half.first + b.half.first);
    double dy = std::abs(a.center.second - b.center.second) - (a.half.second + b.half.second);
    if (dx <= 0.0 && dy <= 0.0) return std::max(dx, dy);
    return std::hypot(std::max(dx, 0.0), std::max(dy, 0.0));
}

Point rotated_aabb_half(double w, double h, double angle_deg) {
    double r = angle_deg * kPi / 180.0;
    double c = std::abs(std::cos(r)), s = std::abs(std::sin(r));
    double hw = w / 2.0, hh = h / 2.0;
    return {hw * c + hh * s, hw * s + hh * c};
}

int n_cu_of(const json& board) {
    int n = 2;
    if (board.contains("stackup") && board["stackup"].is_object() &&
        board["stackup"].contains("copper_layers") && board["stackup"]["copper_layers"].is_number())
        n = board["stackup"]["copper_layers"].get<int>();
    return n;
}

}  // namespace

// Copper layers a pad sits on (drc.py::_pad_layers).
static std::set<std::string> pad_layers(const json& pad, int n_cu) {
    std::set<std::string> cu;
    for (const auto& l : schema::copper_layer_ids(n_cu)) cu.insert(l);
    std::string ptype = pad.value("type", std::string("smd"));
    if (ptype == "tht" || ptype == "np_tht" || ptype == "connector") return cu;
    std::set<std::string> out;
    if (pad.contains("layers") && pad["layers"].is_array()) {
        for (const auto& l : pad["layers"]) {
            if (l.is_string() && cu.count(l.get<std::string>())) out.insert(l.get<std::string>());
        }
    }
    return out;
}

namespace {

bool pad_shape(const json& fp, const json& pad, int n_cu, Shape& out) {
    std::set<std::string> layers = pad_layers(pad, n_cu);
    if (layers.empty()) return false;
    auto [wx, wy] = schema::pad_world(fp, pad);
    double w = 0.0, h = 0.0;
    if (pad.contains("size") && pad["size"].is_array()) {
        const json& size = pad["size"];
        if (size.size() > 0) w = getf(size[0]);
        h = (size.size() > 1) ? getf(size[1]) : w;
    }
    json net = pad.contains("net") ? pad["net"] : json(nullptr);
    std::string pid = pad.value("id", std::string());
    std::string shp = pad.value("shape", std::string("rect"));
    out.net = net;
    out.layers = layers;
    out.item_id = pid;
    out.loc = {wx, wy};
    if (shp == "circle" || shp == "oval") {
        out.kind = "circle";
        out.center = {wx, wy};
        out.margin = std::max(w, h) / 2.0;
        return true;
    }
    double pad_angle = getf(pad.contains("angle") ? pad["angle"] : json(0.0)) +
                       getf(fp.contains("angle") ? fp["angle"] : json(0.0));
    Point hh = rotated_aabb_half(w, h, pad_angle);
    out.kind = "box";
    out.center = {wx, wy};
    out.half = hh;
    return true;
}

bool track_shape(const json& t, Shape& out) {
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
    else if (t.contains("layer") && t["layer"].is_null())
        out.layers.insert(std::string());  // {None}
    out.item_id = t.value("id", std::string());
    out.p1 = p1;
    out.p2 = p2;
    out.margin = width / 2.0;
    out.loc = {(p1.first + p2.first) / 2.0, (p1.second + p2.second) / 2.0};
    return true;
}

void emit_track_shapes(const json& t, std::vector<Shape>& out) {
    const std::string kind = t.value("kind", std::string("segment"));
    if (kind == "segment") {
        Shape shape;
        if (track_shape(t, shape)) out.push_back(std::move(shape));
        return;
    }
    if (kind != "arc") return;

    Point center{0, 0};
    Point p1{0, 0};
    if (t.contains("center") && t["center"].is_array())
        center = {getf(t["center"][0]), getf(t["center"][1])};
    if (t.contains("p1") && t["p1"].is_array())
        p1 = {getf(t["p1"][0]), getf(t["p1"][1])};
    double radius = dist(center, p1);
    if (radius <= kEps && t.contains("radius")) radius = std::abs(getf(t["radius"]));
    const double a0 = getf(t.contains("a0") ? t["a0"] : json(0.0)) * kPi / 180.0;
    const double a1 = getf(t.contains("a1") ? t["a1"] : json(0.0)) * kPi / 180.0;
    const int count = arc_segment_count(a0, a1);
    const double step = (a1 - a0) / count;
    const double sagitta = radius * (1.0 - std::cos(std::abs(step) / 2.0));
    const double margin =
        getf(t.contains("width") ? t["width"] : json(0.0)) / 2.0 + sagitta;

    Point previous{center.first + radius * std::cos(a0),
                   center.second + radius * std::sin(a0)};
    for (int i = 1; i <= count; ++i) {
        const double angle = a0 + step * i;
        Point current{center.first + radius * std::cos(angle),
                      center.second + radius * std::sin(angle)};
        Shape shape;
        shape.kind = "seg";
        shape.net = t.contains("net") ? t["net"] : json(nullptr);
        if (t.contains("layer") && t["layer"].is_string())
            shape.layers.insert(t["layer"].get<std::string>());
        else if (t.contains("layer") && t["layer"].is_null())
            shape.layers.insert(std::string());
        shape.item_id = t.value("id", std::string());
        shape.p1 = previous;
        shape.p2 = current;
        shape.margin = margin;
        shape.loc = {(previous.first + current.first) / 2.0,
                     (previous.second + current.second) / 2.0};
        out.push_back(std::move(shape));
        previous = current;
    }
}

void via_shape(const json& v, Shape& out) {
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

}  // namespace

std::vector<Shape> copper_shapes(const json& board) {
    int n_cu = n_cu_of(board);
    std::vector<Shape> shapes;
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                Shape s;
                if (pad_shape(fp, pad, n_cu, s)) shapes.push_back(std::move(s));
            }
        }
    }
    if (board.contains("tracks") && board["tracks"].is_array()) {
        for (const auto& t : board["tracks"]) emit_track_shapes(t, shapes);
    }
    if (board.contains("vias") && board["vias"].is_array()) {
        for (const auto& v : board["vias"]) {
            Shape s;
            via_shape(v, s);
            shapes.push_back(std::move(s));
        }
    }
    std::stable_sort(shapes.begin(), shapes.end(),
                     [](const Shape& a, const Shape& b) { return a.item_id < b.item_id; });
    return shapes;
}

bool same_net(const json& a, const json& b) {
    if (a.is_null() || b.is_null()) return false;
    if (a.is_string() && a.get<std::string>().empty()) return false;
    if (b.is_string() && b.get<std::string>().empty()) return false;
    return a == b;
}

namespace {

// net-class of a net name (drc.py::_net_class_of) -> the class string, or null.
json net_class_of(const json& board, const json& net) {
    if (board.contains("nets") && board["nets"].is_array()) {
        for (const auto& n : board["nets"]) {
            if (n.is_object() && n.contains("name") && n["name"] == net)
                return n.contains("class") ? n["class"] : json(nullptr);
        }
    }
    return json(nullptr);
}

double clearance_for(const json& board, const Shape& a, const Shape& b) {
    double g = 0.0;
    if (board.contains("rules") && board["rules"].is_object())
        g = getf(board["rules"].contains("clearance") ? board["rules"]["clearance"] : json(0.0));
    json ca = net_class_of(board, a.net);
    json cb = net_class_of(board, b.net);
    if (!ca.is_null() && ca == cb && board.contains("net_classes") &&
        board["net_classes"].is_object() && ca.is_string() &&
        board["net_classes"].contains(ca.get<std::string>())) {
        const json& nc = board["net_classes"][ca.get<std::string>()];
        if (nc.is_object() && nc.contains("clearance"))
            return std::max(getf(nc["clearance"]), 0.0);
    }
    return g;
}

double net_class_minimum(const json& board, const json& net, const char* key,
                         double global_minimum) {
    const json cls = net_class_of(board, net);
    if (!cls.is_string() || !board.contains("net_classes") ||
        !board["net_classes"].is_object() ||
        !board["net_classes"].contains(cls.get<std::string>()))
        return global_minimum;
    const json& nc = board["net_classes"][cls.get<std::string>()];
    if (!nc.is_object() || !nc.contains(key)) return global_minimum;
    return std::max(global_minimum, std::max(getf(nc[key]), 0.0));
}

using PolygonEdge = std::pair<Point, Point>;

std::vector<PolygonEdge> effective_polygon_edges(const std::vector<Point>& points) {
    std::map<PolygonEdge, size_t> counts;
    for (size_t i = 0; i < points.size(); ++i) {
        Point a = points[i];
        Point b = points[(i + 1) % points.size()];
        if (b < a) std::swap(a, b);
        ++counts[{a, b}];
    }
    std::vector<PolygonEdge> edges;
    edges.reserve(counts.size());
    for (const auto& [edge, count] : counts)
        if (count % 2 == 1) edges.push_back(edge);
    return edges;
}

struct FilledZonePolygon {
    std::string zone_id;
    size_t polygon_index = 0;
    json net;
    std::string layer;
    std::vector<Point> points;
    std::vector<PolygonEdge> edges;
    Point loc{0, 0};
    double clearance = 0.0;
};

std::vector<FilledZonePolygon> filled_zone_polygons(const json& board) {
    std::vector<FilledZonePolygon> polygons;
    if (!board.contains("zones") || !board["zones"].is_array()) return polygons;
    for (const auto& zone : board["zones"]) {
        if (!zone.contains("filled_polys") || !zone["filled_polys"].is_array()) continue;
        size_t polygon_index = 0;
        for (const auto& encoded : zone["filled_polys"]) {
            if (!encoded.is_array()) {
                ++polygon_index;
                continue;
            }
            FilledZonePolygon polygon;
            polygon.zone_id = zone.value("id", std::string());
            polygon.polygon_index = polygon_index++;
            polygon.net = zone.contains("net") ? zone["net"] : json(nullptr);
            polygon.layer = zone.value("layer", std::string());
            polygon.clearance =
                std::max(getf(zone.contains("clearance") ? zone["clearance"] : json(0.0)), 0.0);
            for (const auto& encoded_point : encoded) {
                if (!encoded_point.is_array() || encoded_point.size() < 2 ||
                    !encoded_point[0].is_number() || !encoded_point[1].is_number())
                    continue;
                Point point{encoded_point[0].get<double>(), encoded_point[1].get<double>()};
                polygon.points.push_back(point);
                polygon.loc.first += point.first;
                polygon.loc.second += point.second;
            }
            if (polygon.points.size() < 3) continue;
            polygon.edges = effective_polygon_edges(polygon.points);
            polygon.loc.first /= polygon.points.size();
            polygon.loc.second /= polygon.points.size();
            polygons.push_back(std::move(polygon));
        }
    }
    return polygons;
}

bool point_in_polygon(const Point& point, const FilledZonePolygon& polygon) {
    for (const PolygonEdge& edge : polygon.edges)
        if (point_seg_dist(point, edge.first, edge.second) <= kEps) return true;

    bool inside = false;
    size_t previous = polygon.points.size() - 1;
    for (size_t i = 0; i < polygon.points.size(); ++i) {
        const Point& a = polygon.points[previous];
        const Point& b = polygon.points[i];
        const bool crosses = (a.second > point.second) != (b.second > point.second);
        if (crosses) {
            const double x =
                (b.first - a.first) * (point.second - a.second) / (b.second - a.second) +
                a.first;
            if (point.first < x) inside = !inside;
        }
        previous = i;
    }
    return inside;
}

bool point_in_shape(const Point& point, const Shape& shape) {
    if (shape.kind == "circle")
        return dist(point, shape.center) <= shape.margin + kEps;
    if (shape.kind == "seg")
        return point_seg_dist(point, shape.p1, shape.p2) <= shape.margin + kEps;
    return std::abs(point.first - shape.center.first) <= shape.half.first + kEps &&
           std::abs(point.second - shape.center.second) <= shape.half.second + kEps;
}

double polygon_shape_distance(const FilledZonePolygon& polygon, const Shape& shape) {
    double best = std::numeric_limits<double>::infinity();
    for (const PolygonEdge& edge : polygon.edges) {
        const Point& a = edge.first;
        const Point& b = edge.second;
        double d = 0.0;
        if (shape.kind == "seg")
            d = seg_seg_dist(a, b, shape.p1, shape.p2) - shape.margin;
        else if (shape.kind == "circle")
            d = point_seg_dist(shape.center, a, b) - shape.margin;
        else
            d = seg_box_dist(a, b, shape.center.first, shape.center.second,
                             shape.half.first, shape.half.second);
        best = std::min(best, d);
    }

    bool interior_overlap = false;
    for (const Point& point : polygon.points)
        if (point_in_shape(point, shape)) {
            interior_overlap = true;
            break;
        }
    if (shape.kind == "circle") {
        interior_overlap = interior_overlap || point_in_polygon(shape.center, polygon);
    } else if (shape.kind == "seg") {
        interior_overlap = interior_overlap || point_in_polygon(shape.p1, polygon) ||
                           point_in_polygon(shape.p2, polygon);
    } else {
        const double x0 = shape.center.first - shape.half.first;
        const double x1 = shape.center.first + shape.half.first;
        const double y0 = shape.center.second - shape.half.second;
        const double y1 = shape.center.second + shape.half.second;
        const Point corners[4] = {{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}};
        for (const Point& corner : corners)
            interior_overlap = interior_overlap || point_in_polygon(corner, polygon);
    }
    return interior_overlap && best >= 0.0 ? -2.0 * kEps : best;
}

double polygon_polygon_distance(const FilledZonePolygon& a, const FilledZonePolygon& b) {
    if (point_in_polygon(a.points.front(), b) || point_in_polygon(b.points.front(), a))
        return -2.0 * kEps;
    double best = std::numeric_limits<double>::infinity();
    for (const PolygonEdge& edge_a : a.edges)
        for (const PolygonEdge& edge_b : b.edges) {
            const double d =
                seg_seg_dist(edge_a.first, edge_a.second, edge_b.first, edge_b.second);
            if (d <= kEps) return -2.0 * kEps;
            best = std::min(best, d);
        }
    return best;
}

Shape zone_metadata_shape(const FilledZonePolygon& polygon) {
    Shape shape;
    shape.net = polygon.net;
    shape.layers.insert(polygon.layer);
    shape.item_id = polygon.zone_id;
    shape.loc = polygon.loc;
    return shape;
}

// Drilled holes (drc.py::_drilled_holes): {id, at, drill}. Sorted by id.
struct Hole {
    std::string id;
    Point at;
    double drill;
};

std::vector<Hole> drilled_holes(const json& board) {
    std::vector<Hole> holes;
    if (board.contains("vias") && board["vias"].is_array()) {
        for (const auto& v : board["vias"]) {
            double d = getf(v.contains("drill") ? v["drill"] : json(0.0));
            if (d > 0) {
                Point at{0, 0};
                if (v.contains("at") && v["at"].is_array()) at = {getf(v["at"][0]), getf(v["at"][1])};
                holes.push_back({v.value("id", std::string()), at, d});
            }
        }
    }
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                if (!pad.contains("drill") || pad["drill"].is_null()) continue;
                const json& drill = pad["drill"];
                json dia;
                if (drill.is_object()) {
                    if (!drill.contains("diameter")) continue;
                    dia = drill["diameter"];
                } else {
                    dia = drill;
                }
                if (dia.is_null()) continue;
                if (drill.is_object() && (!drill.contains("diameter") || drill["diameter"].is_null()))
                    continue;
                auto [wx, wy] = schema::pad_world(fp, pad);
                holes.push_back({pad.value("id", std::string()), {wx, wy}, getf(dia)});
            }
        }
    }
    std::stable_sort(holes.begin(), holes.end(),
                     [](const Hole& a, const Hole& b) { return a.id < b.id; });
    return holes;
}

// Edge.Cuts outline as line segments (drc.py::_outline_segments).
std::vector<std::pair<Point, Point>> outline_segments(const json& board) {
    std::vector<std::pair<Point, Point>> segs;
    if (!board.contains("outline") || !board["outline"].is_object()) return segs;
    const json& outline = board["outline"];
    if (!outline.contains("shapes") || !outline["shapes"].is_array()) return segs;
    for (const auto& shp : outline["shapes"]) {
        std::string typ = shp.value("type", std::string());
        if (typ == "line") {
            Point p1{0, 0}, p2{0, 0};
            if (shp.contains("p1") && shp["p1"].is_array()) p1 = {getf(shp["p1"][0]), getf(shp["p1"][1])};
            if (shp.contains("p2") && shp["p2"].is_array()) p2 = {getf(shp["p2"][0]), getf(shp["p2"][1])};
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
            int n = arc_segment_count(a0, a1);
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

double shape_edge_dist(const Shape& s, const std::pair<Point, Point>& edge) {
    const Point& a = edge.first;
    const Point& b = edge.second;
    if (s.kind == "seg") return std::max(seg_seg_dist(s.p1, s.p2, a, b) - s.margin, 0.0);
    if (s.kind == "circle") return std::max(point_seg_dist(s.center, a, b) - s.margin, 0.0);
    return seg_box_dist(a, b, s.center.first, s.center.second, s.half.first, s.half.second);
}

json make_violation(const std::string& rule, const std::string& severity,
                    const std::vector<std::tuple<double, double, std::string>>& locs,
                    std::vector<std::string> items, const std::string& message) {
    json jlocs = json::array();
    for (const auto& [x, y, layer] : locs) jlocs.push_back(json::array({x, y, layer}));
    std::sort(items.begin(), items.end());
    return json{{"rule", rule}, {"severity", severity}, {"locs", jlocs},
                {"items", items}, {"message", message}};
}

}  // namespace

json ratsnest(const json& board) {
    json out = json::array();

    // _net_terminals: per-net pads + vias with world position + id (skip empty nets).
    int n_cu = n_cu_of(board);
    struct Terminal {
        std::string id;
        Point at;
    };
    std::map<std::string, std::vector<Terminal>> terminals;  // sorted by net name
    auto net_ok = [](const json& net) {
        return net.is_string() && !net.get<std::string>().empty();
    };
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                json net = pad.contains("net") ? pad["net"] : json(nullptr);
                if (!net_ok(net)) continue;
                auto [wx, wy] = schema::pad_world(fp, pad);
                terminals[net.get<std::string>()].push_back({pad.value("id", std::string()), {wx, wy}});
            }
        }
    }
    if (board.contains("vias") && board["vias"].is_array()) {
        for (const auto& v : board["vias"]) {
            json net = v.contains("net") ? v["net"] : json(nullptr);
            if (!net_ok(net)) continue;
            Point at{0, 0};
            if (v.contains("at") && v["at"].is_array()) at = {getf(v["at"][0]), getf(v["at"][1])};
            terminals[net.get<std::string>()].push_back({v.value("id", std::string()), at});
        }
    }
    for (auto& [net, terms] : terminals)
        std::stable_sort(terms.begin(), terms.end(),
                         [](const Terminal& a, const Terminal& b) { return a.id < b.id; });

    // Tracks grouped by net. An arc is electrically continuous between its declared
    // endpoints; its tessellation is needed for geometry, not for endpoint connectivity.
    std::map<std::string, std::vector<std::pair<Point, Point>>> tracks_by_net;
    if (board.contains("tracks") && board["tracks"].is_array()) {
        for (const auto& t : board["tracks"]) {
            const std::string kind = t.value("kind", std::string("segment"));
            if (kind != "segment" && kind != "arc") continue;
            json net = t.contains("net") ? t["net"] : json(nullptr);
            if (!net_ok(net)) continue;
            Point p1{0, 0}, p2{0, 0};
            if (t.contains("p1") && t["p1"].is_array())
                p1 = {getf(t["p1"][0]), getf(t["p1"][1])};
            if (t.contains("p2") && t["p2"].is_array())
                p2 = {getf(t["p2"][0]), getf(t["p2"][1])};
            tracks_by_net[net.get<std::string>()].push_back({p1, p2});
        }
    }

    // union-find over string keys ("T:id" and "P:x,y").
    struct UF {
        std::map<std::string, std::string> p;
        void add(const std::string& k) {
            if (!p.count(k)) p[k] = k;
        }
        std::string find(const std::string& k) {
            add(k);
            std::string root = k;
            while (p[root] != root) root = p[root];
            std::string cur = k;
            while (p[cur] != root) {
                std::string nxt = p[cur];
                p[cur] = root;
                cur = nxt;
            }
            return root;
        }
        void uni(const std::string& a, const std::string& b) {
            std::string ra = find(a), rb = find(b);
            if (ra != rb) p[rb] = ra;
        }
    };

    // endpoint key: round to 1e6 grid (matches Python _ep_key).
    auto ep_key = [](const Point& q) {
        double x = std::round(q.first * 1e6) / 1e6 + 0.0;
        double y = std::round(q.second * 1e6) / 1e6 + 0.0;
        char buf[128];
        std::snprintf(buf, sizeof(buf), "P:%.17g,%.17g", x, y);
        return std::string(buf);
    };

    for (auto& [net, terms] : terminals) {
        if (terms.size() < 2) continue;
        UF uf;
        for (const auto& r : terms) uf.add("T:" + r.id);
        const auto& segs = tracks_by_net.count(net) ? tracks_by_net[net]
                                                    : std::vector<std::pair<Point, Point>>();
        for (const auto& seg : segs) uf.uni(ep_key(seg.first), ep_key(seg.second));
        for (const auto& r : terms) {
            for (const auto& seg : segs) {
                if (dist(r.at, seg.first) <= kTouch || dist(r.at, seg.second) <= kTouch) {
                    uf.uni("T:" + r.id, ep_key(seg.first));
                    uf.uni("T:" + r.id, ep_key(seg.second));
                }
            }
        }
        for (size_t i = 0; i < terms.size(); ++i)
            for (size_t j = i + 1; j < terms.size(); ++j)
                if (dist(terms[i].at, terms[j].at) <= kTouch)
                    uf.uni("T:" + terms[i].id, "T:" + terms[j].id);

        // components of terminals keyed by UF root.
        std::map<std::string, std::vector<Terminal>> comp;
        for (const auto& r : terms) comp[uf.find("T:" + r.id)].push_back(r);
        // reps sorted by min terminal id in each component.
        std::vector<std::string> reps;
        for (auto& [root, cterms] : comp) reps.push_back(root);
        auto min_id = [&](const std::string& root) {
            std::string m = comp[root][0].id;
            for (const auto& t : comp[root]) m = std::min(m, t.id);
            return m;
        };
        std::sort(reps.begin(), reps.end(),
                  [&](const std::string& a, const std::string& b) { return min_id(a) < min_id(b); });
        if (reps.size() < 2) continue;

        auto comp_dist = [&](const std::string& ca, const std::string& cb) {
            double best = std::numeric_limits<double>::infinity();
            const Terminal* bta = nullptr;
            const Terminal* btb = nullptr;
            for (const auto& ta : comp[ca])
                for (const auto& tb : comp[cb]) {
                    double d = dist(ta.at, tb.at);
                    if (d < best) {
                        best = d;
                        bta = &ta;
                        btb = &tb;
                    }
                }
            return std::make_tuple(best, bta, btb);
        };

        // Prim from reps[0].
        std::set<std::string> in_tree{reps[0]};
        std::set<std::string> remaining(reps.begin() + 1, reps.end());
        while (!remaining.empty()) {
            std::vector<std::string> in_sorted(in_tree.begin(), in_tree.end());
            std::sort(in_sorted.begin(), in_sorted.end(),
                      [&](const std::string& a, const std::string& b) { return min_id(a) < min_id(b); });
            std::vector<std::string> rem_sorted(remaining.begin(), remaining.end());
            std::sort(rem_sorted.begin(), rem_sorted.end(),
                      [&](const std::string& a, const std::string& b) { return min_id(a) < min_id(b); });
            double best = std::numeric_limits<double>::infinity();
            std::string best_to;
            const Terminal* bta = nullptr;
            const Terminal* btb = nullptr;
            for (const auto& fr : in_sorted) {
                for (const auto& to : rem_sorted) {
                    auto [d, ta, tb] = comp_dist(fr, to);
                    if (d < best) {
                        best = d;
                        best_to = to;
                        bta = ta;
                        btb = tb;
                    }
                }
            }
            out.push_back(json{{"net", net},
                               {"p1", json::array({bta->at.first, bta->at.second})},
                               {"p2", json::array({btb->at.first, btb->at.second})}});
            in_tree.insert(best_to);
            remaining.erase(best_to);
        }
    }

    // deterministic order: (net, p1, p2)
    std::sort(out.begin(), out.end(), [](const json& a, const json& b) {
        std::string na = a["net"].get<std::string>(), nb = b["net"].get<std::string>();
        if (na != nb) return na < nb;
        std::vector<double> a1{a["p1"][0].get<double>(), a["p1"][1].get<double>()};
        std::vector<double> b1{b["p1"][0].get<double>(), b["p1"][1].get<double>()};
        if (a1 != b1) return a1 < b1;
        std::vector<double> a2{a["p2"][0].get<double>(), a["p2"][1].get<double>()};
        std::vector<double> b2{b["p2"][0].get<double>(), b["p2"][1].get<double>()};
        return a2 < b2;
    });
    return out;
}

json run_drc(const json& board) {
    json out = json::array();
    if (!schema::is_board(board)) return out;

    const json rules = board.contains("rules") && board["rules"].is_object() ? board["rules"]
                                                                             : json::object();

    std::vector<Shape> shapes = copper_shapes(board);

    // clearance
    std::set<std::tuple<std::string, std::string, std::string>> reported_clearance_pairs;
    for (size_t i = 0; i < shapes.size(); ++i) {
        for (size_t k = i + 1; k < shapes.size(); ++k) {
            const Shape& a = shapes[i];
            const Shape& b = shapes[k];
            if (same_net(a.net, b.net)) continue;
            if (!shared_layer(a, b)) continue;
            double req = clearance_for(board, a, b);
            double d = edge_distance(a, b);
            if (d < req - kEps) {
                std::set<std::string> common;
                for (const auto& l : a.layers)
                    if (b.layers.count(l)) common.insert(l);
                std::string layer = *common.begin();
                const std::string first_id = std::min(a.item_id, b.item_id);
                const std::string second_id = std::max(a.item_id, b.item_id);
                if (!reported_clearance_pairs.insert({first_id, second_id, layer}).second)
                    continue;
                std::string msg = "clearance " + fmt4g(d) + "mm < required " + fmt4g(req) +
                                  "mm between " + a.item_id + " (net " + repr_net(a.net) + ") and " +
                                  b.item_id + " (net " + repr_net(b.net) + ")";
                out.push_back(make_violation(
                    "clearance", "error",
                    {{a.loc.first, a.loc.second, layer}, {b.loc.first, b.loc.second, layer}},
                    {a.item_id, b.item_id}, msg));
            }
        }
    }

    const std::vector<FilledZonePolygon> zone_polygons = filled_zone_polygons(board);

    // zone_clearance: filled copper is authoritative stale state and must be checked
    // against every different-net copper item, independent of the zone-fill operation.
    for (const FilledZonePolygon& polygon : zone_polygons) {
        const Shape zone_shape = zone_metadata_shape(polygon);
        std::set<std::string> reported_items;
        for (const Shape& shape : shapes) {
            if (same_net(polygon.net, shape.net) || !shape.layers.count(polygon.layer))
                continue;
            const double required =
                std::max(polygon.clearance, clearance_for(board, zone_shape, shape));
            const double distance = polygon_shape_distance(polygon, shape);
            if (distance >= required - kEps || reported_items.count(shape.item_id)) continue;
            reported_items.insert(shape.item_id);
            const std::string message =
                "zone " + polygon.zone_id + " polygon " +
                std::to_string(polygon.polygon_index) + " clearance " + fmt4g(distance) +
                "mm < required " + fmt4g(required) + "mm to " + shape.item_id + " (net " +
                repr_net(shape.net) + ")";
            out.push_back(make_violation(
                "zone_clearance", "error",
                {{polygon.loc.first, polygon.loc.second, polygon.layer},
                 {shape.loc.first, shape.loc.second, polygon.layer}},
                {polygon.zone_id, shape.item_id}, message));
        }
    }
    for (size_t i = 0; i < zone_polygons.size(); ++i) {
        for (size_t k = i + 1; k < zone_polygons.size(); ++k) {
            const FilledZonePolygon& a = zone_polygons[i];
            const FilledZonePolygon& b = zone_polygons[k];
            if (same_net(a.net, b.net) || a.layer != b.layer) continue;
            const Shape a_shape = zone_metadata_shape(a);
            const Shape b_shape = zone_metadata_shape(b);
            const double required =
                std::max(std::max(a.clearance, b.clearance),
                         clearance_for(board, a_shape, b_shape));
            const double distance = polygon_polygon_distance(a, b);
            if (distance >= required - kEps) continue;
            const std::string message =
                "zone " + a.zone_id + " polygon " + std::to_string(a.polygon_index) +
                " clearance " + fmt4g(distance) + "mm < required " + fmt4g(required) +
                "mm to zone " + b.zone_id + " polygon " +
                std::to_string(b.polygon_index);
            out.push_back(make_violation(
                "zone_clearance", "error",
                {{a.loc.first, a.loc.second, a.layer}, {b.loc.first, b.loc.second, b.layer}},
                {a.zone_id, b.zone_id}, message));
        }
    }

    // zone_island: a polygon is connected when it touches own-net copper directly or
    // through a same-net filled polygon on the same layer.
    std::vector<bool> zone_connected(zone_polygons.size(), false);
    for (size_t i = 0; i < zone_polygons.size(); ++i) {
        const FilledZonePolygon& polygon = zone_polygons[i];
        for (const Shape& shape : shapes) {
            if (!same_net(polygon.net, shape.net) || !shape.layers.count(polygon.layer))
                continue;
            if (polygon_shape_distance(polygon, shape) <= kTouch) {
                zone_connected[i] = true;
                break;
            }
        }
    }
    bool changed = true;
    while (changed) {
        changed = false;
        for (size_t i = 0; i < zone_polygons.size(); ++i) {
            for (size_t k = i + 1; k < zone_polygons.size(); ++k) {
                if (zone_connected[i] == zone_connected[k]) continue;
                const FilledZonePolygon& a = zone_polygons[i];
                const FilledZonePolygon& b = zone_polygons[k];
                if (!same_net(a.net, b.net) || a.layer != b.layer ||
                    polygon_polygon_distance(a, b) > kTouch)
                    continue;
                zone_connected[i] = true;
                zone_connected[k] = true;
                changed = true;
            }
        }
    }
    for (size_t i = 0; i < zone_polygons.size(); ++i) {
        if (zone_connected[i]) continue;
        const FilledZonePolygon& polygon = zone_polygons[i];
        const std::string message =
            "zone " + polygon.zone_id + " polygon " +
            std::to_string(polygon.polygon_index) +
            " is floating copper with no connection to net " + repr_net(polygon.net);
        out.push_back(make_violation(
            "zone_island", "warning",
            {{polygon.loc.first, polygon.loc.second, polygon.layer}}, {polygon.zone_id},
            message));
    }

    // track_width
    double tw_min = getf(rules.contains("track_width_min") ? rules["track_width_min"] : json(0.0));
    if (board.contains("tracks") && board["tracks"].is_array()) {
        for (const auto& t : board["tracks"]) {
            double w = getf(t.contains("width") ? t["width"] : json(0.0));
            const json net = t.contains("net") ? t["net"] : json(nullptr);
            const double effective_min = net_class_minimum(board, net, "track_width", tw_min);
            if (w < effective_min - kEps) {
                Point p1{0, 0}, p2{0, 0};
                if (t.contains("p1") && t["p1"].is_array())
                    p1 = {getf(t["p1"][0]), getf(t["p1"][1])};
                if (t.contains("p2") && t["p2"].is_array())
                    p2 = {getf(t["p2"][0]), getf(t["p2"][1])};
                Point mid{(p1.first + p2.first) / 2.0, (p1.second + p2.second) / 2.0};
                std::string layer = t.value("layer", std::string("F.Cu"));
                std::string tid = t.value("id", std::string());
                std::string msg = "track " + tid + " width " + fmt4g(w) + "mm < minimum " +
                                  fmt4g(effective_min) + "mm";
                out.push_back(make_violation("track_width", "error",
                                             {{mid.first, mid.second, layer}}, {tid}, msg));
            }
        }
    }

    // via_size
    double dia_min = getf(rules.contains("via_diameter_min") ? rules["via_diameter_min"] : json(0.0));
    double drill_min = getf(rules.contains("via_drill_min") ? rules["via_drill_min"] : json(0.0));
    if (board.contains("vias") && board["vias"].is_array()) {
        for (const auto& v : board["vias"]) {
            Point at{0, 0};
            if (v.contains("at") && v["at"].is_array())
                at = {getf(v["at"][0]), getf(v["at"][1])};
            double dia = getf(v.contains("diameter") ? v["diameter"] : json(0.0));
            double drill = getf(v.contains("drill") ? v["drill"] : json(0.0));
            std::string vid = v.value("id", std::string());
            const json net = v.contains("net") ? v["net"] : json(nullptr);
            const double effective_dia_min =
                net_class_minimum(board, net, "via_diameter", dia_min);
            const double effective_drill_min =
                net_class_minimum(board, net, "via_drill", drill_min);
            if (dia < effective_dia_min - kEps) {
                out.push_back(make_violation(
                    "via_size", "error", {{at.first, at.second, "F.Cu"}}, {vid},
                    "via " + vid + " diameter " + fmt4g(dia) + "mm < minimum " +
                        fmt4g(effective_dia_min) + "mm"));
            }
            if (drill < effective_drill_min - kEps) {
                out.push_back(make_violation(
                    "via_size", "error", {{at.first, at.second, "F.Cu"}}, {vid},
                    "via " + vid + " drill " + fmt4g(drill) + "mm < minimum " +
                        fmt4g(effective_drill_min) + "mm"));
            }
        }
    }

    // annular_ring (vias + tht pads)
    double ar_min = getf(rules.contains("annular_ring_min") ? rules["annular_ring_min"] : json(0.0));
    if (board.contains("vias") && board["vias"].is_array()) {
        for (const auto& v : board["vias"]) {
            Point at{0, 0};
            if (v.contains("at") && v["at"].is_array()) at = {getf(v["at"][0]), getf(v["at"][1])};
            double ring = (getf(v.contains("diameter") ? v["diameter"] : json(0.0)) -
                           getf(v.contains("drill") ? v["drill"] : json(0.0))) / 2.0;
            std::string vid = v.value("id", std::string());
            if (ring < ar_min - kEps) {
                out.push_back(make_violation(
                    "annular_ring", "error", {{at.first, at.second, "F.Cu"}}, {vid},
                    "via " + vid + " annular ring " + fmt4g(ring) + "mm < minimum " + fmt4g(ar_min) + "mm"));
            }
        }
    }
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                if (!pad.contains("drill") || pad["drill"].is_null()) continue;
                const json& drill = pad["drill"];
                json dia;
                if (drill.is_object()) {
                    if (!drill.contains("diameter")) continue;
                    dia = drill["diameter"];
                } else {
                    dia = drill;
                }
                if (dia.is_null()) continue;
                double w = 0, h = 0;
                bool have2 = false;
                if (pad.contains("size") && pad["size"].is_array()) {
                    const json& size = pad["size"];
                    if (size.size() > 0) w = getf(size[0]);
                    if (size.size() > 1) {
                        h = getf(size[1]);
                        have2 = true;
                    }
                }
                double pad_min_dim = have2 ? std::min(w, h) : w;
                double ring = (pad_min_dim - getf(dia)) / 2.0;
                if (ring < ar_min - kEps) {
                    auto [wx, wy] = schema::pad_world(fp, pad);
                    std::string pid = pad.value("id", std::string());
                    out.push_back(make_violation(
                        "annular_ring", "error", {{wx, wy, "F.Cu"}}, {pid},
                        "pad " + pid + " annular ring " + fmt4g(ring) + "mm < minimum " +
                            fmt4g(ar_min) + "mm"));
                }
            }
        }
    }

    // hole_to_hole
    double h2h_min = getf(rules.contains("hole_to_hole_min") ? rules["hole_to_hole_min"] : json(0.0));
    std::vector<Hole> holes = drilled_holes(board);
    for (size_t i = 0; i < holes.size(); ++i) {
        for (size_t k = i + 1; k < holes.size(); ++k) {
            const Hole& ha = holes[i];
            const Hole& hb = holes[k];
            double gap = dist(ha.at, hb.at) - ha.drill / 2.0 - hb.drill / 2.0;
            if (gap < h2h_min - kEps) {
                out.push_back(make_violation(
                    "hole_to_hole", "error",
                    {{ha.at.first, ha.at.second, "F.Cu"}, {hb.at.first, hb.at.second, "F.Cu"}},
                    {ha.id, hb.id},
                    "hole-to-hole gap " + fmt4g(gap) + "mm < minimum " + fmt4g(h2h_min) +
                        "mm between " + ha.id + " and " + hb.id));
            }
        }
    }

    // edge_clearance
    double edge_min = getf(rules.contains("edge_clearance") ? rules["edge_clearance"] : json(0.0));
    auto edges = outline_segments(board);
    if (!edges.empty()) {
        std::set<std::string> reported_edge_items;
        for (const auto& s : shapes) {
            double best = std::numeric_limits<double>::infinity();
            for (const auto& e : edges) best = std::min(best, shape_edge_dist(s, e));
            if (best < edge_min - kEps && reported_edge_items.insert(s.item_id).second) {
                std::string lyr = s.layers.empty() ? "F.Cu" : *s.layers.begin();
                out.push_back(make_violation(
                    "edge_clearance", "error", {{s.loc.first, s.loc.second, lyr}}, {s.item_id},
                    "copper " + s.item_id + " is " + fmt4g(best) +
                        "mm from the board edge (< " + fmt4g(edge_min) + "mm)"));
            }
        }
    }

    // unrouted (one warning per ratsnest airwire)
    for (const auto& air : ratsnest(board)) {
        double p1x = air["p1"][0].get<double>(), p1y = air["p1"][1].get<double>();
        double p2x = air["p2"][0].get<double>(), p2y = air["p2"][1].get<double>();
        std::string net = air["net"].get<std::string>();
        std::string msg = "net '" + net + "' has an unrouted airwire (" + fmt4g(p1x) + "," +
                          fmt4g(p1y) + ") -> (" + fmt4g(p2x) + "," + fmt4g(p2y) + ")";
        out.push_back(make_violation("unrouted", "warning",
                                     {{p1x, p1y, "F.Cu"}, {p2x, p2y, "F.Cu"}}, {}, msg));
    }

    // deterministic final ordering: (rule, severity, items, locs, message)
    std::stable_sort(out.begin(), out.end(), [](const json& a, const json& b) {
        if (a["rule"] != b["rule"]) return a["rule"].get<std::string>() < b["rule"].get<std::string>();
        if (a["severity"] != b["severity"])
            return a["severity"].get<std::string>() < b["severity"].get<std::string>();
        // items: list of strings, lexicographic
        const json& ai = a["items"];
        const json& bi = b["items"];
        for (size_t i = 0; i < std::min(ai.size(), bi.size()); ++i) {
            if (ai[i] != bi[i]) return ai[i].get<std::string>() < bi[i].get<std::string>();
        }
        if (ai.size() != bi.size()) return ai.size() < bi.size();
        // locs: list of [float, float, str]; compare element-wise
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
