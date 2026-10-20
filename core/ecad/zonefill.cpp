// Copper zone fill implementation — see zonefill.h. Mirrors engine/ecad/zonefill.py
// vertex-for-vertex: numpy grid (cell-centre sampling), even-odd PIP, DRC-shape
// clearance carving, 4-spoke thermal relief, min_width island drop, and the
// 4-connected component boundary trace with keyholed holes. Pure JSON/geometry.

#include "ecad/zonefill.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "ecad/drc.h"
#include "ecad/schema.h"

namespace chudware::ecad {

using nlohmann::json;

namespace {

constexpr double kPi = 3.14159265358979323846;

using Point = std::pair<double, double>;

double getf(const json& v, double dflt = 0.0) {
    return v.is_number() ? v.get<double>() : dflt;
}

int n_cu_of(const json& board) {
    int n = 2;
    if (board.contains("stackup") && board["stackup"].is_object() &&
        board["stackup"].contains("copper_layers") && board["stackup"]["copper_layers"].is_number())
        n = board["stackup"]["copper_layers"].get<int>();
    return n;
}

// even-odd point-in-polygon for a single point (mirrors _points_in_polygon scalar).
bool point_in_polygon(double xs, double ys, const std::vector<Point>& poly) {
    int n = (int)poly.size();
    bool inside = false;
    int j = n - 1;
    for (int i = 0; i < n; ++i) {
        double xi = poly[i].first, yi = poly[i].second;
        double xj = poly[j].first, yj = poly[j].second;
        bool cond = (yi > ys) != (yj > ys);
        double denom = yj - yi;
        if (denom == 0.0) denom = 1e-30;
        double xint = (xj - xi) * (ys - yi) / denom + xi;
        if (cond && (xs < xint)) inside = !inside;
        j = i;
    }
    return inside;
}

std::vector<Point> json_to_poly(const json& jp) {
    std::vector<Point> poly;
    for (const auto& p : jp) poly.push_back({getf(p[0]), getf(p[1])});
    return poly;
}

void poly_bbox(const std::vector<Point>& poly, double& minx, double& miny, double& maxx, double& maxy) {
    minx = miny = std::numeric_limits<double>::infinity();
    maxx = maxy = -std::numeric_limits<double>::infinity();
    for (const auto& p : poly) {
        minx = std::min(minx, p.first);
        miny = std::min(miny, p.second);
        maxx = std::max(maxx, p.first);
        maxy = std::max(maxy, p.second);
    }
}

// ---- copper obstacle model (reuse DRC's shape abstraction) ----

std::vector<Shape> layer_shapes(const json& board, const std::string& layer) {
    std::vector<Shape> out;
    for (auto& s : copper_shapes(board))
        if (s.layers.count(layer)) out.push_back(std::move(s));
    return out;
}

double point_to_shape_dist(double px, double py, const Shape& s) {
    if (s.kind == "circle")
        return std::max(std::hypot(px - s.center.first, py - s.center.second) - s.margin, 0.0);
    if (s.kind == "box") {
        double dx = std::max(std::abs(px - s.center.first) - s.half.first, 0.0);
        double dy = std::max(std::abs(py - s.center.second) - s.half.second, 0.0);
        return std::hypot(dx, dy);
    }
    // seg: point to centreline minus half-width
    double ax = s.p1.first, ay = s.p1.second, bx = s.p2.first, by = s.p2.second;
    double dx = bx - ax, dy = by - ay;
    double dd = dx * dx + dy * dy;
    double d;
    if (dd <= 1e-9) {
        d = std::hypot(px - ax, py - ay);
    } else {
        double t = ((px - ax) * dx + (py - ay) * dy) / dd;
        t = t < 0.0 ? 0.0 : (t > 1.0 ? 1.0 : t);
        d = std::hypot(px - (ax + t * dx), py - (ay + t * dy));
    }
    return std::max(d - s.margin, 0.0);
}

struct CellRange {
    int begin, end;  // half-open
};

// Search the actual cell centres instead of converting world coordinates to int:
// distant/huge bounds cannot overflow a cast, and negative origins or rounding
// of min + (i + 0.5) * res do not move a boundary cell out of the range.
CellRange cell_range(const std::vector<double>& centres, double lo, double hi, double res) {
    const CellRange all{0, static_cast<int>(centres.size())};
    if (!std::isfinite(lo) || !std::isfinite(hi) || lo > hi || !std::isfinite(res))
        return all;
    // One cell plus an arithmetic guard keeps this a broad phase only. The
    // latter also covers segment projection's ax + t * (bx - ax) cancellation.
    const double guard = res + 8.0 * std::numeric_limits<double>::epsilon() *
                                   std::max(std::abs(lo), std::abs(hi));
    lo = std::nextafter(lo - guard, -std::numeric_limits<double>::infinity());
    hi = std::nextafter(hi + guard, std::numeric_limits<double>::infinity());
    if (!std::isfinite(lo) || !std::isfinite(hi)) return all;
    return {static_cast<int>(std::lower_bound(centres.begin(), centres.end(), lo) - centres.begin()),
            static_cast<int>(std::upper_bound(centres.begin(), centres.end(), hi) - centres.begin())};
}

struct CellRect {
    CellRange rows, cols;
};

CellRect clearance_cells(const Shape& s, double req, const std::vector<double>& cx,
                         const std::vector<double>& cy, double res) {
    const CellRect all{{0, static_cast<int>(cy.size())}, {0, static_cast<int>(cx.size())}};
    if (!std::isfinite(req)) return all;
    double x0, y0, x1, y1;
    if (s.kind == "circle" || s.kind == "box") {
        if (!std::isfinite(s.center.first) || !std::isfinite(s.center.second)) return all;
        double hx, hy;
        if (s.kind == "circle") {
            if (!std::isfinite(s.margin)) return all;
            hx = hy = std::max(s.margin, 0.0);
        } else {
            if (!std::isfinite(s.half.first) || !std::isfinite(s.half.second)) return all;
            hx = std::max(s.half.first, 0.0);
            hy = std::max(s.half.second, 0.0);
        }
        x0 = s.center.first - hx;
        x1 = s.center.first + hx;
        y0 = s.center.second - hy;
        y1 = s.center.second + hy;
    } else {
        if (!std::isfinite(s.p1.first) || !std::isfinite(s.p1.second) ||
            !std::isfinite(s.p2.first) || !std::isfinite(s.p2.second) ||
            !std::isfinite(s.margin))
            return all;
        const double radius = std::max(s.margin, 0.0);
        x0 = std::min(s.p1.first, s.p2.first) - radius;
        x1 = std::max(s.p1.first, s.p2.first) + radius;
        y0 = std::min(s.p1.second, s.p2.second) - radius;
        y1 = std::max(s.p1.second, s.p2.second) + radius;
    }
    const double clearance = std::max(req, 0.0);
    return {cell_range(cy, y0 - clearance, y1 + clearance, res),
            cell_range(cx, x0 - clearance, x1 + clearance, res)};
}

json net_class_of(const json& board, const json& net) {
    if (board.contains("nets") && board["nets"].is_array()) {
        for (const auto& n : board["nets"]) {
            if (n.is_object() && n.contains("name") && n["name"] == net)
                return n.contains("class") ? n["class"] : json(nullptr);
        }
    }
    return json(nullptr);
}

double net_class_clearance(const json& board, const json& net) {
    json cls = net_class_of(board, net);
    if (!cls.is_null() && cls.is_string() && board.contains("net_classes") &&
        board["net_classes"].is_object() && board["net_classes"].contains(cls.get<std::string>())) {
        const json& nc = board["net_classes"][cls.get<std::string>()];
        if (nc.is_object() && nc.contains("clearance")) return std::max(getf(nc["clearance"]), 0.0);
    }
    return 0.0;
}

double pair_clearance(const json& board, const json& net_a, const json& net_b) {
    double g = 0.0;
    if (board.contains("rules") && board["rules"].is_object())
        g = getf(board["rules"].contains("clearance") ? board["rules"]["clearance"] : json(0.0));
    json ca = net_class_of(board, net_a);
    json cb = net_class_of(board, net_b);
    if (!ca.is_null() && ca == cb && ca.is_string() && board.contains("net_classes") &&
        board["net_classes"].is_object() && board["net_classes"].contains(ca.get<std::string>())) {
        const json& nc = board["net_classes"][ca.get<std::string>()];
        if (nc.is_object() && nc.contains("clearance")) return std::max(getf(nc["clearance"]), g);
    }
    return g;
}

// ---- thermal relief ----

struct ThermalPad {
    double cx, cy, r_copper, r_gap, spoke_half;
    bool voids(double px, double py) const {
        double dx = px - cx, dy = py - cy;
        double d = std::hypot(dx, dy);
        if (d >= r_gap || d <= r_copper) return false;
        bool in_spoke = (std::abs(dy) <= spoke_half) || (std::abs(dx) <= spoke_half);
        return !in_spoke;
    }
};

std::set<std::string> pad_layers_of(const json& pad, int n_cu) {
    std::set<std::string> cu;
    for (const auto& l : schema::copper_layer_ids(n_cu)) cu.insert(l);
    std::string ptype = pad.value("type", std::string("smd"));
    if (ptype == "tht" || ptype == "np_tht" || ptype == "connector") return cu;
    std::set<std::string> out;
    if (pad.contains("layers") && pad["layers"].is_array())
        for (const auto& l : pad["layers"])
            if (l.is_string() && cu.count(l.get<std::string>())) out.insert(l.get<std::string>());
    return out;
}

std::vector<ThermalPad> thermal_pads(const json& board, const json& zone, const std::string& layer,
                                     const json& net) {
    json thermal = (zone.contains("thermal") && zone["thermal"].is_object()) ? zone["thermal"]
                                                                            : json::object();
    double gap = getf(thermal.contains("gap") ? thermal["gap"] : json(0.0));
    double spoke_w = getf(thermal.contains("spoke_width") ? thermal["spoke_width"] : json(0.0));
    int n_cu = n_cu_of(board);
    std::vector<ThermalPad> out;
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                json pnet = pad.contains("net") ? pad["net"] : json(nullptr);
                if (!same_net(pnet, net)) continue;
                std::string ptype = pad.value("type", std::string("smd"));
                if (ptype != "tht" && ptype != "np_tht" && ptype != "connector") continue;
                if (!pad_layers_of(pad, n_cu).count(layer)) continue;
                auto [wx, wy] = schema::pad_world(fp, pad);
                double w = 0, h = 0;
                if (pad.contains("size") && pad["size"].is_array()) {
                    const json& size = pad["size"];
                    if (size.size() > 0) w = getf(size[0]);
                    h = (size.size() > 1) ? getf(size[1]) : w;
                }
                double r_copper = std::max(w, h) / 2.0;
                ThermalPad tp;
                tp.cx = wx;
                tp.cy = wy;
                tp.r_copper = r_copper;
                tp.r_gap = r_copper + std::max(gap, 0.0);
                tp.spoke_half = std::max(spoke_w, 0.0) / 2.0;
                out.push_back(tp);
            }
        }
    }
    std::stable_sort(out.begin(), out.end(), [](const ThermalPad& a, const ThermalPad& b) {
        if (a.cx != b.cx) return a.cx < b.cx;
        return a.cy < b.cy;
    });
    return out;
}

// ---- outline polygons ----

std::vector<std::vector<Point>> outline_segments_chain(const json& board) {
    // mirror drc._outline_segments then chain into closed loops (zonefill._outline_polygons)
    std::vector<std::pair<Point, Point>> segs;
    if (board.contains("outline") && board["outline"].is_object() &&
        board["outline"].contains("shapes") && board["outline"]["shapes"].is_array()) {
        for (const auto& shp : board["outline"]["shapes"]) {
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
    }
    std::vector<std::vector<Point>> loops;
    if (segs.empty()) return loops;
    double tol = 1e-6;
    std::vector<std::pair<Point, Point>> remaining = segs;
    while (!remaining.empty()) {
        auto [a, b] = remaining.front();
        remaining.erase(remaining.begin());
        std::vector<Point> loop{a, b};
        bool changed = true;
        while (changed && !remaining.empty()) {
            changed = false;
            Point end = loop.back();
            for (size_t k = 0; k < remaining.size(); ++k) {
                const Point& c = remaining[k].first;
                const Point& d = remaining[k].second;
                if (std::abs(c.first - end.first) < tol && std::abs(c.second - end.second) < tol) {
                    loop.push_back(d);
                    remaining.erase(remaining.begin() + k);
                    changed = true;
                    break;
                }
                if (std::abs(d.first - end.first) < tol && std::abs(d.second - end.second) < tol) {
                    loop.push_back(c);
                    remaining.erase(remaining.begin() + k);
                    changed = true;
                    break;
                }
            }
        }
        if (loop.size() >= 3) {
            if (std::abs(loop.front().first - loop.back().first) < tol &&
                std::abs(loop.front().second - loop.back().second) < tol)
                loop.pop_back();
            loops.push_back(loop);
        }
    }
    return loops;
}

// ---- contour tracing ----

double signed_area(const std::vector<Point>& loop) {
    double s = 0.0;
    int n = (int)loop.size();
    for (int i = 0; i < n; ++i) {
        double x1 = loop[i].first, y1 = loop[i].second;
        double x2 = loop[(i + 1) % n].first, y2 = loop[(i + 1) % n].second;
        s += x1 * y2 - x2 * y1;
    }
    return s / 2.0;
}

std::vector<Point> simplify_collinear(const std::vector<Point>& loop) {
    if (loop.size() < 3) return loop;
    std::vector<Point> out;
    int n = (int)loop.size();
    for (int i = 0; i < n; ++i) {
        const Point& a = loop[(i - 1 + n) % n];
        const Point& b = loop[i];
        const Point& c = loop[(i + 1) % n];
        double cross = (b.first - a.first) * (c.second - b.second) -
                       (b.second - a.second) * (c.first - b.first);
        if (std::abs(cross) > 1e-12) out.push_back(b);
    }
    return out.empty() ? loop : out;
}

// boundary loops of a 4-connected component in grid-corner coords (mirrors _boundary_loops).
bool boundary_loops(const std::vector<std::vector<bool>>& comp, int ny, int nx,
                    std::vector<Point>& outer, std::vector<std::vector<Point>>& holes) {
    // Adjacency: ordered list of out-edges per vertex; pop() is LIFO (matches Python list.pop()).
    std::map<Point, std::vector<Point>> adj;
    auto add_edge = [&](const Point& p, const Point& q) { adj[p].push_back(q); };
    for (int r = 0; r < ny; ++r) {
        for (int c = 0; c < nx; ++c) {
            if (!comp[r][c]) continue;
            Point bl{(double)c, (double)r};
            Point br{(double)(c + 1), (double)r};
            Point tr{(double)(c + 1), (double)(r + 1)};
            Point tl{(double)c, (double)(r + 1)};
            if (r == 0 || !comp[r - 1][c]) add_edge(bl, br);
            if (c == nx - 1 || !comp[r][c + 1]) add_edge(br, tr);
            if (r == ny - 1 || !comp[r + 1][c]) add_edge(tr, tl);
            if (c == 0 || !comp[r][c - 1]) add_edge(tl, bl);
        }
    }
    // chain edges into closed loops. Python iterates dict in INSERTION order to find a start.
    // Reproduce insertion order with a parallel key list.
    std::vector<Point> key_order;
    {
        std::set<Point> seen;
        for (int r = 0; r < ny; ++r)
            for (int c = 0; c < nx; ++c) {
                if (!comp[r][c]) continue;
                Point bl{(double)c, (double)r}, br{(double)(c + 1), (double)r};
                Point tr{(double)(c + 1), (double)(r + 1)}, tl{(double)c, (double)(r + 1)};
                auto note = [&](const Point& p, bool present) {
                    if (present && !seen.count(p)) {
                        seen.insert(p);
                        key_order.push_back(p);
                    }
                };
                note(bl, r == 0 || !comp[r - 1][c]);
                note(br, c == nx - 1 || !comp[r][c + 1]);
                note(tr, r == ny - 1 || !comp[r + 1][c]);
                note(tl, c == 0 || !comp[r][c - 1]);
            }
    }
    long total = 0;
    for (auto& kv : adj) total += (long)kv.second.size();
    long used = 0;
    std::vector<std::vector<Point>> loops;
    size_t start_index = 0;
    while (used < total) {
        // Exhausted keys never regain edges. Retain the insertion-order frontier
        // instead of rescanning every earlier contour for each new loop.
        Point start;
        bool found = false;
        while (start_index < key_order.size()) {
            const Point& k = key_order[start_index];
            auto it = adj.find(k);
            if (it != adj.end() && !it->second.empty()) {
                start = k;
                found = true;
                break;
            }
            ++start_index;
        }
        if (!found) break;
        std::vector<Point> loop{start};
        Point cur = start;
        while (true) {
            auto it = adj.find(cur);
            if (it == adj.end() || it->second.empty()) break;
            Point nxt = it->second.back();
            it->second.pop_back();
            ++used;
            loop.push_back(nxt);
            cur = nxt;
            if (cur == start) break;
        }
        std::vector<Point> trimmed = (loop.back() == loop.front())
                                         ? std::vector<Point>(loop.begin(), loop.end() - 1)
                                         : loop;
        loops.push_back(simplify_collinear(trimmed));
    }
    if (loops.empty()) return false;
    // outer = max abs signed area (stable sort descending by abs area)
    std::vector<std::pair<double, std::vector<Point>>> areas;
    for (auto& lp : loops) areas.push_back({signed_area(lp), lp});
    std::stable_sort(areas.begin(), areas.end(),
                     [](const auto& a, const auto& b) { return std::abs(a.first) > std::abs(b.first); });
    outer = areas[0].second;
    if (signed_area(outer) < 0) std::reverse(outer.begin(), outer.end());
    holes.clear();
    for (size_t i = 1; i < areas.size(); ++i) {
        std::vector<Point> h = areas[i].second;
        if (!(areas[i].first < 0)) std::reverse(h.begin(), h.end());
        holes.push_back(h);
    }
    return true;
}

std::vector<Point> bridge_hole(const std::vector<Point>& outer, const std::vector<Point>& hole) {
    double best = std::numeric_limits<double>::infinity();
    int bi = 0, bj = 0;
    for (int i = 0; i < (int)outer.size(); ++i)
        for (int j = 0; j < (int)hole.size(); ++j) {
            double d = (outer[i].first - hole[j].first) * (outer[i].first - hole[j].first) +
                       (outer[i].second - hole[j].second) * (outer[i].second - hole[j].second);
            if (d < best) {
                best = d;
                bi = i;
                bj = j;
            }
        }
    std::vector<Point> hole_seq;
    for (int k = bj; k < (int)hole.size(); ++k) hole_seq.push_back(hole[k]);
    for (int k = 0; k < bj; ++k) hole_seq.push_back(hole[k]);
    hole_seq.push_back(hole[bj]);
    std::vector<Point> out;
    for (int k = 0; k <= bi; ++k) out.push_back(outer[k]);
    for (const auto& p : hole_seq) out.push_back(p);
    out.push_back(outer[bi]);
    for (int k = bi + 1; k < (int)outer.size(); ++k) out.push_back(outer[k]);
    return out;
}

std::vector<Point> trace_component(const std::vector<std::vector<bool>>& comp, int ny, int nx,
                                   double x0, double y0, double res) {
    std::vector<Point> outer;
    std::vector<std::vector<Point>> holes;
    if (!boundary_loops(comp, ny, nx, outer, holes)) return {};
    std::vector<Point> ring = outer;
    for (auto& hole : holes) ring = bridge_hole(ring, hole);
    std::vector<Point> world;
    for (const auto& p : ring) world.push_back({x0 + p.first * res, y0 + p.second * res});
    return world;
}

// mask -> polygons (mirrors _mask_to_polygons): 4-conn labelling, drop < min_cells, trace.
json mask_to_polygons(const std::vector<std::vector<bool>>& mask, int ny, int nx, double x0,
                      double y0, double res, int min_cells) {
    json polys = json::array();
    std::vector<std::vector<int>> labels(ny, std::vector<int>(nx, 0));
    int cur = 0;
    for (int sr = 0; sr < ny; ++sr) {
        for (int sc = 0; sc < nx; ++sc) {
            if (!mask[sr][sc] || labels[sr][sc]) continue;
            ++cur;
            std::vector<std::pair<int, int>> stack{{sr, sc}};
            labels[sr][sc] = cur;
            int count = 0;
            const int dr[4] = {1, -1, 0, 0};
            const int dc[4] = {0, 0, 1, -1};
            while (!stack.empty()) {
                auto [r, c] = stack.back();
                stack.pop_back();
                ++count;
                for (int k = 0; k < 4; ++k) {
                    int nr = r + dr[k], nc = c + dc[k];
                    if (nr >= 0 && nr < ny && nc >= 0 && nc < nx && mask[nr][nc] && !labels[nr][nc]) {
                        labels[nr][nc] = cur;
                        stack.push_back({nr, nc});
                    }
                }
            }
            if (count < min_cells) {
                for (int r = 0; r < ny; ++r)
                    for (int c = 0; c < nx; ++c)
                        if (labels[r][c] == cur) labels[r][c] = 0;
            }
        }
    }
    for (int lab = 1; lab <= cur; ++lab) {
        std::vector<std::vector<bool>> comp(ny, std::vector<bool>(nx, false));
        bool any = false;
        for (int r = 0; r < ny; ++r)
            for (int c = 0; c < nx; ++c)
                if (labels[r][c] == lab) {
                    comp[r][c] = true;
                    any = true;
                }
        if (!any) continue;
        std::vector<Point> ring = trace_component(comp, ny, nx, x0, y0, res);
        if (ring.size() >= 3) {
            json jp = json::array();
            for (const auto& p : ring) jp.push_back(json::array({p.first, p.second}));
            polys.push_back(jp);
        }
    }
    return polys;
}

}  // namespace

json fill_zone(const json& board, const json& zone, double resolution) {
    if (!zone.contains("polygon") || !zone["polygon"].is_array() || zone["polygon"].size() < 3)
        return json::array();
    std::vector<Point> poly = json_to_poly(zone["polygon"]);
    std::string layer = zone.value("layer", std::string());
    json net = zone.contains("net") ? zone["net"] : json(nullptr);

    double rules_clear = 0.0;
    if (board.contains("rules") && board["rules"].is_object())
        rules_clear = getf(board["rules"].contains("clearance") ? board["rules"]["clearance"] : json(0.0));
    double base_clear = std::max(
        std::max(getf(zone.contains("clearance") ? zone["clearance"] : json(0.0)), rules_clear),
        net_class_clearance(board, net));

    double minx, miny, maxx, maxy;
    poly_bbox(poly, minx, miny, maxx, maxy);
    auto outline_polys = outline_segments_chain(board);
    if (!outline_polys.empty()) {
        double oxmin = std::numeric_limits<double>::infinity();
        double oymin = std::numeric_limits<double>::infinity();
        double oxmax = -std::numeric_limits<double>::infinity();
        double oymax = -std::numeric_limits<double>::infinity();
        for (const auto& op : outline_polys)
            for (const auto& p : op) {
                oxmin = std::min(oxmin, p.first);
                oymin = std::min(oymin, p.second);
                oxmax = std::max(oxmax, p.first);
                oymax = std::max(oymax, p.second);
            }
        minx = std::max(minx, oxmin);
        miny = std::max(miny, oymin);
        maxx = std::min(maxx, oxmax);
        maxy = std::min(maxy, oymax);
    }
    if (maxx <= minx || maxy <= miny) return json::array();

    double res = resolution;
    int nx = std::max(1, (int)std::ceil((maxx - minx) / res));
    int ny = std::max(1, (int)std::ceil((maxy - miny) / res));
    const long MAX_CELLS = 4000000;
    if ((long)nx * ny > MAX_CELLS) {
        double scale = std::sqrt((double)nx * ny / MAX_CELLS);
        res *= scale;
        nx = std::max(1, (int)std::ceil((maxx - minx) / res));
        ny = std::max(1, (int)std::ceil((maxy - miny) / res));
    }

    std::vector<double> cx(nx), cy(ny);
    for (int i = 0; i < nx; ++i) cx[i] = minx + (i + 0.5) * res;
    for (int i = 0; i < ny; ++i) cy[i] = miny + (i + 0.5) * res;

    // 1) inside zone polygon  (mask[r][c])
    std::vector<std::vector<bool>> mask(ny, std::vector<bool>(nx, false));
    for (int r = 0; r < ny; ++r)
        for (int c = 0; c < nx; ++c) mask[r][c] = point_in_polygon(cx[c], cy[r], poly);

    // 2) inside board outline (even-odd union of fills)
    if (!outline_polys.empty()) {
        for (int r = 0; r < ny; ++r)
            for (int c = 0; c < nx; ++c) {
                bool inside = false;
                for (const auto& op : outline_polys)
                    if (point_in_polygon(cx[c], cy[r], op)) inside = !inside;
                mask[r][c] = mask[r][c] && inside;
            }
    }

    // 3) subtract clearance around DIFFERENT-net copper on this layer
    std::vector<Shape> shapes = layer_shapes(board, layer);
    for (const auto& s : shapes) {
        if (same_net(s.net, net)) continue;
        double req = std::max(base_clear, pair_clearance(board, net, s.net));
        const CellRect cells = clearance_cells(s, req, cx, cy, res);
        for (int r = cells.rows.begin; r < cells.rows.end; ++r)
            for (int c = cells.cols.begin; c < cells.cols.end; ++c)
                if (mask[r][c] && point_to_shape_dist(cx[c], cy[r], s) < req) mask[r][c] = false;
    }

    // 4) thermal relief
    auto tpads = thermal_pads(board, zone, layer, net);
    for (const auto& tp : tpads) {
        CellRect cells{{0, ny}, {0, nx}};
        // A nonfinite coordinate can make hypot NaN, for which voids() need
        // not reject points outside r_gap. Preserve that predicate's behavior.
        if (std::isfinite(tp.cx) && std::isfinite(tp.cy) && std::isfinite(tp.r_copper) &&
            std::isfinite(tp.r_gap) && std::isfinite(tp.spoke_half)) {
            cells = {cell_range(cy, tp.cy - tp.r_gap, tp.cy + tp.r_gap, res),
                     cell_range(cx, tp.cx - tp.r_gap, tp.cx + tp.r_gap, res)};
        }
        for (int r = cells.rows.begin; r < cells.rows.end; ++r)
            for (int c = cells.cols.begin; c < cells.cols.end; ++c)
                if (mask[r][c] && tp.voids(cx[c], cy[r])) mask[r][c] = false;
    }

    // 5) trace to polygons, dropping islands below min_width^2
    double min_width = getf(zone.contains("min_width") ? zone["min_width"] : json(0.0));
    double min_island_area = std::max(min_width * min_width, 0.0);
    int min_cells =
        min_island_area > 0 ? std::max(1, (int)std::floor(min_island_area / (res * res))) : 1;

    return mask_to_polygons(mask, ny, nx, minx, miny, res, min_cells);
}

json fill_zones(const json& board, double resolution) {
    json out = board;  // deep copy
    if (!schema::is_board(out)) return out;
    std::set<std::string> cu;
    for (const auto& l : schema::copper_layer_ids(n_cu_of(out))) cu.insert(l);
    if (out.contains("zones") && out["zones"].is_array()) {
        for (auto& zone : out["zones"]) {
            std::string layer = zone.value("layer", std::string());
            if (!cu.count(layer)) {
                zone["filled_polys"] = json::array();
                continue;
            }
            zone["filled_polys"] = fill_zone(out, zone, resolution);
        }
    }
    return out;
}

}  // namespace chudware::ecad
