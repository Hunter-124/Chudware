// Inscribed-sphere wall thickness. See wall_thickness.h for the measurement definition.
#include "geom/wall_thickness.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>
#include <unordered_map>

#include "util/thread_pool.h"

namespace chudware::geom {

namespace {

constexpr float kNaN = std::numeric_limits<float>::quiet_NaN();
constexpr double kPi = 3.14159265358979323846;

inline double dot3(const double a[3], const double b[3]) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

// Static kd-tree over a flat float xyz array (median split on the widest axis). Holds a
// reference to the array, which must outlive the tree.
class PointTree {
public:
    explicit PointTree(const std::vector<float>& pts) : pts_(pts) {
        const auto n = static_cast<uint32_t>(pts.size() / 3);
        perm_.resize(n);
        std::iota(perm_.begin(), perm_.end(), 0u);
        if (n > 0) {
            nodes_.reserve(2 * (n / kLeaf) + 2);
            build(0, n);
        }
    }

    // Tighten (best, best2) to the nearest point closer than sqrt(best2) that `accept`
    // admits; leaves both untouched when there is none. Chains across several trees.
    template <class Accept>
    void nearest(const double q[3], int64_t& best, double& best2, const Accept& accept) const {
        if (!nodes_.empty()) nearest_rec(0, q, best, best2, accept);
    }

    // fn(index, squared distance) for every point within sqrt(r2) of q.
    template <class Fn>
    void within(const double q[3], double r2, const Fn& fn) const {
        if (!nodes_.empty()) within_rec(0, q, r2, fn);
    }

private:
    static constexpr uint32_t kLeaf = 12;
    struct Node {
        float lo[3], hi[3];
        uint32_t begin = 0, end = 0;
        int32_t left = -1, right = -1;
    };

    int32_t build(uint32_t b, uint32_t e) {
        Node node;
        node.begin = b;
        node.end = e;
        for (int k = 0; k < 3; ++k) {
            node.lo[k] = std::numeric_limits<float>::infinity();
            node.hi[k] = -std::numeric_limits<float>::infinity();
        }
        for (uint32_t i = b; i < e; ++i)
            for (int k = 0; k < 3; ++k) {
                const float v = pts_[3 * static_cast<std::size_t>(perm_[i]) + k];
                node.lo[k] = std::min(node.lo[k], v);
                node.hi[k] = std::max(node.hi[k], v);
            }
        const auto id = static_cast<int32_t>(nodes_.size());
        nodes_.push_back(node);
        if (e - b > kLeaf) {
            int axis = 0;
            for (int k = 1; k < 3; ++k)
                if (node.hi[k] - node.lo[k] > node.hi[axis] - node.lo[axis]) axis = k;
            const uint32_t mid = b + (e - b) / 2;
            std::nth_element(perm_.begin() + b, perm_.begin() + mid, perm_.begin() + e,
                             [&](uint32_t x, uint32_t y) {
                                 return pts_[3 * static_cast<std::size_t>(x) + axis] <
                                        pts_[3 * static_cast<std::size_t>(y) + axis];
                             });
            const int32_t l = build(b, mid);
            const int32_t r = build(mid, e);
            nodes_[static_cast<std::size_t>(id)].left = l;
            nodes_[static_cast<std::size_t>(id)].right = r;
        }
        return id;
    }

    static double box_d2(const Node& n, const double q[3]) {
        double d2 = 0.0;
        for (int k = 0; k < 3; ++k) {
            const double d = q[k] < n.lo[k] ? n.lo[k] - q[k] : (q[k] > n.hi[k] ? q[k] - n.hi[k] : 0.0);
            d2 += d * d;
        }
        return d2;
    }

    double point_d2(uint32_t i, const double q[3]) const {
        const float* p = &pts_[3 * static_cast<std::size_t>(i)];
        const double dx = p[0] - q[0], dy = p[1] - q[1], dz = p[2] - q[2];
        return dx * dx + dy * dy + dz * dz;
    }

    template <class Accept>
    void nearest_rec(int32_t id, const double q[3], int64_t& best, double& best2,
                     const Accept& accept) const {
        const Node& n = nodes_[static_cast<std::size_t>(id)];
        if (box_d2(n, q) >= best2) return;
        if (n.left < 0) {
            for (uint32_t i = n.begin; i < n.end; ++i) {
                const uint32_t k = perm_[i];
                const double d2 = point_d2(k, q);
                if (d2 < best2 && accept(k)) {
                    best2 = d2;
                    best = k;
                }
            }
            return;
        }
        const double dl = box_d2(nodes_[static_cast<std::size_t>(n.left)], q);
        const double dr = box_d2(nodes_[static_cast<std::size_t>(n.right)], q);
        if (dl <= dr) {
            nearest_rec(n.left, q, best, best2, accept);
            nearest_rec(n.right, q, best, best2, accept);
        } else {
            nearest_rec(n.right, q, best, best2, accept);
            nearest_rec(n.left, q, best, best2, accept);
        }
    }

    template <class Fn>
    void within_rec(int32_t id, const double q[3], double r2, const Fn& fn) const {
        const Node& n = nodes_[static_cast<std::size_t>(id)];
        if (box_d2(n, q) > r2) return;
        if (n.left < 0) {
            for (uint32_t i = n.begin; i < n.end; ++i) {
                const uint32_t k = perm_[i];
                const double d2 = point_d2(k, q);
                if (d2 <= r2) fn(k, d2);
            }
            return;
        }
        within_rec(n.left, q, r2, fn);
        within_rec(n.right, q, r2, fn);
    }

    const std::vector<float>& pts_;
    std::vector<uint32_t> perm_;
    std::vector<Node> nodes_;
};

// The wall samples split by normal direction (cube-map cells, 4x4 per cube face), one
// kd-tree per cell. A ball grows straight through the faces that cannot oppose it (its own
// face, the faces beside a convex edge), and a single tree would walk every one of those
// samples inside the ball on every shrink step; a cell whose whole normal cone stays within
// the separation angle of the query normal is never searched.
class NormalBins {
public:
    NormalBins(const std::vector<float>& pos, const std::vector<float>& nrm) {
        std::vector<std::vector<uint32_t>> members(kBins);
        for (std::size_t i = 0; 3 * i + 2 < nrm.size(); ++i)
            members[bin_of(&nrm[3 * i])].push_back(static_cast<uint32_t>(i));
        for (std::vector<uint32_t>& m : members) {
            if (m.empty()) continue;
            Bin bin;
            double mean[3] = {0, 0, 0};
            for (const uint32_t i : m)
                for (int k = 0; k < 3; ++k) mean[k] += nrm[3 * i + k];
            const double len = std::sqrt(dot3(mean, mean));
            for (int k = 0; k < 3; ++k) bin.axis[k] = len > 0 ? mean[k] / len : 0.0;
            double min_cos = 1.0;
            for (const uint32_t i : m) {
                const double n[3] = {nrm[3 * i], nrm[3 * i + 1], nrm[3 * i + 2]};
                min_cos = std::min(min_cos, dot3(n, bin.axis));
                bin.pts.insert(bin.pts.end(), &pos[3 * i], &pos[3 * i] + 3);
            }
            bin.spread = std::acos(std::clamp(min_cos, -1.0, 1.0));
            bin.index = std::move(m);
            bins_.push_back(std::move(bin));
        }
        // Trees reference their bin's points: build them once the bins stop moving.
        trees_.reserve(bins_.size());
        for (const Bin& b : bins_) trees_.emplace_back(b.pts);
    }

    // Cells that may hold a sample q with angle(n, n_q) >= separation (radians).
    void candidates(const double n[3], double separation, std::vector<uint32_t>& out) const {
        out.clear();
        for (std::size_t b = 0; b < bins_.size(); ++b) {
            const double angle = std::acos(std::clamp(dot3(n, bins_[b].axis), -1.0, 1.0));
            if (angle + bins_[b].spread >= separation - 1e-9) out.push_back(static_cast<uint32_t>(b));
        }
    }

    // Nearest sample (global index) among `cells` closer than sqrt(best2) that `accept`
    // admits; -1 if none.
    template <class Accept>
    int64_t nearest(const double q[3], double best2, const std::vector<uint32_t>& cells,
                    const Accept& accept) const {
        int64_t best = -1;
        for (const uint32_t b : cells) {
            const std::vector<uint32_t>& index = bins_[b].index;
            int64_t local = -1;
            trees_[b].nearest(q, local, best2,
                              [&](uint32_t k) { return accept(index[k]); });
            if (local >= 0) best = index[static_cast<std::size_t>(local)];
        }
        return best;
    }

private:
    static constexpr int kCells = 4;
    static constexpr int kBins = 6 * kCells * kCells;
    struct Bin {
        double axis[3] = {0, 0, 0};
        double spread = 0.0;  // largest angle (radians) from axis to a member normal
        std::vector<float> pts;
        std::vector<uint32_t> index;  // bin-local -> global sample index
    };

    static int bin_of(const float* n) {
        int axis = 0;
        for (int k = 1; k < 3; ++k)
            if (std::abs(n[k]) > std::abs(n[axis])) axis = k;
        const float m = std::abs(n[axis]);
        if (!(m > 0.0f)) return 0;
        const int face = 2 * axis + (n[axis] < 0.0f ? 1 : 0);
        const auto cell = [&](float c) {
            const int i = static_cast<int>((c / m + 1.0f) * 0.5f * kCells);
            return std::clamp(i, 0, kCells - 1);
        };
        return face * kCells * kCells + cell(n[(axis + 1) % 3]) * kCells + cell(n[(axis + 2) % 3]);
    }

    std::vector<Bin> bins_;
    std::vector<PointTree> trees_;
};

// parallel_for over [0, n) in fixed-size chunks.
template <class Fn>
void for_chunks(std::size_t n, const Fn& fn) {
    constexpr std::size_t kChunk = 512;
    const auto chunks = static_cast<int>((n + kChunk - 1) / kChunk);
    chudware::pool::shared().parallel_for(chunks, [&](int c) {
        const std::size_t b = static_cast<std::size_t>(c) * kChunk;
        const std::size_t e = std::min(n, b + kChunk);
        for (std::size_t i = b; i < e; ++i) fn(i);
    });
}

struct UnionFind {
    explicit UnionFind(std::size_t n) : parent(n) { std::iota(parent.begin(), parent.end(), 0u); }
    uint32_t find(uint32_t x) {
        while (parent[x] != x) {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        return x;
    }
    void unite(uint32_t a, uint32_t b) {
        a = find(a);
        b = find(b);
        if (a != b) parent[std::max(a, b)] = std::min(a, b);
    }
    std::vector<uint32_t> parent;
};

}  // namespace

double mesh_area(const ThicknessMesh& mesh) {
    double area = 0.0;
    for (const double a : vertex_areas(mesh)) area += a;
    return area;
}

std::vector<double> vertex_areas(const ThicknessMesh& mesh) {
    std::vector<double> areas(mesh.n_verts(), 0.0);
    for (std::size_t t = 0; t < mesh.n_tris(); ++t) {
        const uint32_t* v = &mesh.idx[3 * t];
        double e1[3], e2[3];
        for (int k = 0; k < 3; ++k) {
            e1[k] = static_cast<double>(mesh.pos[3 * v[1] + k]) - mesh.pos[3 * v[0] + k];
            e2[k] = static_cast<double>(mesh.pos[3 * v[2] + k]) - mesh.pos[3 * v[0] + k];
        }
        const double cx = e1[1] * e2[2] - e1[2] * e2[1];
        const double cy = e1[2] * e2[0] - e1[0] * e2[2];
        const double cz = e1[0] * e2[1] - e1[1] * e2[0];
        const double third = std::sqrt(cx * cx + cy * cy + cz * cz) / 6.0;
        for (int k = 0; k < 3; ++k) areas[v[k]] += third;
    }
    return areas;
}

ThicknessMesh refine_mesh(const ThicknessMesh& mesh, double spacing) {
    ThicknessMesh out;
    out.pos = mesh.pos;
    out.nrm = mesh.nrm;
    struct Tri {
        uint32_t v[3];
        uint32_t group;
    };
    std::vector<Tri> tris(mesh.n_tris()), next;
    for (std::size_t t = 0; t < tris.size(); ++t)
        tris[t] = Tri{{mesh.idx[3 * t], mesh.idx[3 * t + 1], mesh.idx[3 * t + 2]},
                      mesh.tri_group[t]};
    if (spacing > 0.0) {
        const double h2 = spacing * spacing;
        std::unordered_map<uint64_t, uint32_t> mids;
        const auto key = [](uint32_t a, uint32_t b) {
            return (static_cast<uint64_t>(std::min(a, b)) << 32) | std::max(a, b);
        };
        const auto len2 = [&](uint32_t a, uint32_t b) {
            double s = 0.0;
            for (int k = 0; k < 3; ++k) {
                const double d = static_cast<double>(out.pos[3 * b + k]) - out.pos[3 * a + k];
                s += d * d;
            }
            return s;
        };
        const auto midpoint = [&](uint32_t a, uint32_t b) {
            const auto [it, fresh] = mids.try_emplace(key(a, b), 0u);
            if (!fresh) return it->second;
            double pa[3], pb[3], na[3], nb[3], nm[3];
            for (int k = 0; k < 3; ++k) {
                pa[k] = out.pos[3 * a + k];
                pb[k] = out.pos[3 * b + k];
                na[k] = out.nrm[3 * a + k];
                nb[k] = out.nrm[3 * b + k];
                nm[k] = na[k] + nb[k];
            }
            double len = std::sqrt(dot3(nm, nm));
            if (len < 1e-12) {
                for (int k = 0; k < 3; ++k) nm[k] = na[k];
                len = 1.0;
            }
            for (double& c : nm) c /= len;
            double dn[3], dp[3];
            for (int k = 0; k < 3; ++k) {
                dn[k] = nb[k] - na[k];
                dp[k] = pb[k] - pa[k];
            }
            const double lift = dot3(dn, dp) / 8.0;
            const auto id = static_cast<uint32_t>(out.pos.size() / 3);
            for (int k = 0; k < 3; ++k) {
                out.pos.push_back(static_cast<float>(0.5 * (pa[k] + pb[k]) + lift * nm[k]));
                out.nrm.push_back(static_cast<float>(nm[k]));
            }
            it->second = id;
            return id;
        };
        // Each pass bisects every triangle whose longest edge is too long or that carries
        // a neighbour's midpoint on any edge; children replace their parent in place, so
        // per-group triangle runs stay contiguous. Longest-edge bisection terminates: an
        // edge holding a pending midpoint becomes the longest edge of the shrinking child
        // that contains it.
        for (int pass = 0; pass < 256; ++pass) {
            bool changed = false;
            next.clear();
            next.reserve(tris.size() + tris.size() / 2);
            for (const Tri& t : tris) {
                int k = 0;
                double best = -1.0;
                bool pending = false;
                for (int e = 0; e < 3; ++e) {
                    const uint32_t a = t.v[e], b = t.v[(e + 1) % 3];
                    const double l2 = len2(a, b);
                    if (l2 > best) {
                        best = l2;
                        k = e;
                    }
                    pending = pending || mids.count(key(a, b)) != 0;
                }
                if (best <= h2 && !pending) {
                    next.push_back(t);
                    continue;
                }
                const uint32_t a = t.v[k], b = t.v[(k + 1) % 3], c = t.v[(k + 2) % 3];
                const uint32_t m = midpoint(a, b);
                next.push_back(Tri{{a, m, c}, t.group});
                next.push_back(Tri{{m, b, c}, t.group});
                changed = true;
            }
            tris.swap(next);
            if (!changed) break;
        }
    }
    out.idx.reserve(3 * tris.size());
    out.tri_group.reserve(tris.size());
    for (const Tri& t : tris) {
        out.idx.insert(out.idx.end(), t.v, t.v + 3);
        out.tri_group.push_back(t.group);
    }
    return out;
}

ThicknessResult measure_wall_thickness(const std::vector<float>& blocker_pos,
                                       const std::vector<float>& blocker_nrm,
                                       const std::vector<float>& query_pos,
                                       const std::vector<float>& query_nrm,
                                       const ThicknessOptions& options) {
    const std::size_t nq = query_pos.size() / 3;
    ThicknessResult res;
    res.thickness.assign(nq, kNaN);
    res.opposing_cos.assign(nq, kNaN);
    res.opposite.assign(3 * nq, kNaN);
    res.center.assign(3 * nq, kNaN);
    if (nq == 0 || blocker_pos.size() < 3) return res;

    // Starting radius: the diagonal of everything, so every ball starts out enclosing
    // whatever wall it can reach.
    double lo[3] = {1e300, 1e300, 1e300}, hi[3] = {-1e300, -1e300, -1e300};
    for (const auto* set : {&blocker_pos, &query_pos})
        for (std::size_t i = 0; i + 2 < set->size(); i += 3)
            for (int k = 0; k < 3; ++k) {
                lo[k] = std::min(lo[k], static_cast<double>((*set)[i + k]));
                hi[k] = std::max(hi[k], static_cast<double>((*set)[i + k]));
            }
    const double diag = std::sqrt((hi[0] - lo[0]) * (hi[0] - lo[0]) +
                                  (hi[1] - lo[1]) * (hi[1] - lo[1]) +
                                  (hi[2] - lo[2]) * (hi[2] - lo[2]));
    const double r0 = std::max(diag, 1e-6);
    const double separation = options.wall_separation_deg * kPi / 180.0;
    const double cos_sep = std::cos(separation);
    const NormalBins blockers(blocker_pos, blocker_nrm);
    // A sample must sit this far (relative) inside the ball to shrink it. Without the margin
    // a ball inside a cylinder or sphere, which touches a whole ring of samples at once,
    // creeps down by float noise for dozens of steps; 1e-4 caps the error at 0.02%.
    constexpr double kInside = 1.0 - 1e-4;

    std::vector<float> radius(nq, kNaN);
    for_chunks(nq, [&](std::size_t i) {
        const double p[3] = {query_pos[3 * i], query_pos[3 * i + 1], query_pos[3 * i + 2]};
        double n[3] = {query_nrm[3 * i], query_nrm[3 * i + 1], query_nrm[3 * i + 2]};
        const double nl = std::sqrt(dot3(n, n));
        if (!(nl > 1e-12)) return;
        for (double& c : n) c /= nl;
        const auto opposes = [&](uint32_t k) {
            const float* m = &blocker_nrm[3 * static_cast<std::size_t>(k)];
            return n[0] * m[0] + n[1] * m[1] + n[2] * m[2] <= cos_sep;
        };
        thread_local std::vector<uint32_t> cells;
        blockers.candidates(n, separation, cells);
        // Shrinking ball: shrink to the ball through p and the nearest opposing sample
        // inside, until none is inside.
        double r = r0;
        int64_t wall = -1;
        for (int it = 0; it < 64; ++it) {
            const double c[3] = {p[0] - n[0] * r, p[1] - n[1] * r, p[2] - n[2] * r};
            const int64_t k = blockers.nearest(c, r * r * kInside * kInside, cells, opposes);
            if (k < 0) break;
            const float* q = &blocker_pos[3 * static_cast<std::size_t>(k)];
            const double d[3] = {p[0] - q[0], p[1] - q[1], p[2] - q[2]};
            const double nd = dot3(n, d);
            if (!(nd > 1e-12)) break;
            const double rn = dot3(d, d) / (2.0 * nd);
            if (!(rn < r)) break;
            r = rn;
            wall = k;
        }
        if (wall < 0) return;
        // The converged sample sits up to half a spacing off the true contact point, which
        // overstates r by ~s^2/2t. The ball tangent to the wall's tangent PLANE at that
        // sample is exact for flat walls and second-order for curved ones.
        const float* q = &blocker_pos[3 * static_cast<std::size_t>(wall)];
        const float* m = &blocker_nrm[3 * static_cast<std::size_t>(wall)];
        const double nq_dot = n[0] * m[0] + n[1] * m[1] + n[2] * m[2];
        const double rp = ((q[0] - p[0]) * m[0] + (q[1] - p[1]) * m[1] + (q[2] - p[2]) * m[2]) /
                          (1.0 - nq_dot);
        if (std::isfinite(rp) && rp > 0.5 * r) r = std::min(r, rp);
        radius[i] = static_cast<float>(r);
        res.thickness[i] = static_cast<float>(2.0 * r);
        res.opposing_cos[i] = static_cast<float>(nq_dot);
        for (int k = 0; k < 3; ++k) {
            res.opposite[3 * i + k] = q[k];
            res.center[3 * i + k] = static_cast<float>(p[k] - n[k] * r);
        }
    });

    if (options.coverage_radius_mm <= 0.0) return res;

    // Coverage: a point inside a smaller ball than its own sits in thinner material than
    // its own ball measured (rib tops, pin ends, the faces beside a thin web).
    std::vector<uint32_t> ball_of;
    std::vector<float> centers;
    for (std::size_t i = 0; i < nq; ++i)
        if (std::isfinite(radius[i]) && radius[i] <= options.coverage_radius_mm) {
            ball_of.push_back(static_cast<uint32_t>(i));
            centers.insert(centers.end(), &res.center[3 * i], &res.center[3 * i] + 3);
        }
    if (ball_of.empty()) return res;
    const PointTree balls(centers);
    const double tol = std::max(options.coverage_tolerance_mm, 0.0);
    std::vector<int64_t> cover(nq, -1);
    for_chunks(nq, [&](std::size_t i) {
        const double x[3] = {query_pos[3 * i], query_pos[3 * i + 1], query_pos[3 * i + 2]};
        double best = std::isfinite(radius[i]) ? static_cast<double>(radius[i])
                                               : std::numeric_limits<double>::infinity();
        const double reach = std::min(best, options.coverage_radius_mm) + tol;
        int64_t pick = -1;
        balls.within(x, reach * reach, [&](uint32_t j, double d2) {
            const uint32_t b = ball_of[j];
            const double rb = radius[b];
            if (rb < best && d2 <= (rb + tol) * (rb + tol)) {
                best = rb;
                pick = b;
            }
        });
        cover[i] = pick;
    });
    for (std::size_t i = 0; i < nq; ++i) {
        const int64_t b = cover[i];
        if (b < 0) continue;
        res.thickness[i] = res.thickness[static_cast<std::size_t>(b)];
        res.opposing_cos[i] = res.opposing_cos[static_cast<std::size_t>(b)];
        for (int k = 0; k < 3; ++k) {
            res.opposite[3 * i + k] = res.opposite[3 * static_cast<std::size_t>(b) + k];
            res.center[3 * i + k] = res.center[3 * static_cast<std::size_t>(b) + k];
        }
    }
    return res;
}

std::vector<ThinRegion> thin_regions(const ThicknessMesh& mesh,
                                     const std::vector<float>& thickness,
                                     const std::vector<double>& areas, double threshold,
                                     double link_mm) {
    std::vector<uint32_t> group_of(mesh.n_verts(), 0u);
    for (std::size_t t = 0; t < mesh.n_tris(); ++t)
        for (int k = 0; k < 3; ++k) group_of[mesh.idx[3 * t + k]] = mesh.tri_group[t];

    std::vector<uint32_t> thin;
    std::vector<float> pts;
    for (std::size_t v = 0; v < thickness.size(); ++v)
        if (std::isfinite(thickness[v]) && thickness[v] < threshold) {
            thin.push_back(static_cast<uint32_t>(v));
            pts.insert(pts.end(), &mesh.pos[3 * v], &mesh.pos[3 * v] + 3);
        }
    if (thin.empty()) return {};

    UnionFind uf(thin.size());
    const PointTree tree(pts);
    const double link2 = link_mm * link_mm;
    for (std::size_t i = 0; i < thin.size(); ++i) {
        const double q[3] = {pts[3 * i], pts[3 * i + 1], pts[3 * i + 2]};
        tree.within(q, link2, [&](uint32_t j, double) {
            if (j > i) uf.unite(static_cast<uint32_t>(i), j);
        });
    }

    std::unordered_map<uint32_t, std::size_t> slot;
    std::vector<ThinRegion> regions;
    for (std::size_t i = 0; i < thin.size(); ++i) {
        const uint32_t root = uf.find(static_cast<uint32_t>(i));
        const auto [it, fresh] = slot.try_emplace(root, regions.size());
        if (fresh) {
            ThinRegion r;
            r.min_thickness = std::numeric_limits<float>::infinity();
            for (int k = 0; k < 3; ++k) {
                r.lo[k] = std::numeric_limits<float>::infinity();
                r.hi[k] = -std::numeric_limits<float>::infinity();
            }
            regions.push_back(std::move(r));
        }
        ThinRegion& r = regions[it->second];
        const uint32_t v = thin[i];
        if (thickness[v] < r.min_thickness) {
            r.min_thickness = thickness[v];
            r.min_vertex = v;
        }
        const double a = areas[v];
        r.area += a;
        for (int k = 0; k < 3; ++k) {
            const float c = mesh.pos[3 * v + k];
            r.centroid[k] += a * c;
            r.lo[k] = std::min(r.lo[k], c);
            r.hi[k] = std::max(r.hi[k], c);
        }
        r.groups.push_back(group_of[v]);
    }
    for (ThinRegion& r : regions) {
        for (int k = 0; k < 3; ++k)
            r.centroid[k] = r.area > 0.0 ? r.centroid[k] / r.area
                                         : 0.5 * (static_cast<double>(r.lo[k]) + r.hi[k]);
        std::sort(r.groups.begin(), r.groups.end());
        r.groups.erase(std::unique(r.groups.begin(), r.groups.end()), r.groups.end());
    }
    std::sort(regions.begin(), regions.end(), [](const ThinRegion& a, const ThinRegion& b) {
        return a.min_thickness < b.min_thickness;
    });
    return regions;
}

}  // namespace chudware::geom
