// core/harness/geometry.cpp — see geometry.h.
#include "harness/geometry.h"

#include <algorithm>
#include <numeric>
#include <utility>

namespace chudware::harness {
namespace {

constexpr size_t kLeafSize = 4;
constexpr int kStackSize = 128;

inline double clamp01(double t) { return t < 0.0 ? 0.0 : (t > 1.0 ? 1.0 : t); }

inline V3 tri_a(const TriF& t) { return V3(t.v[0], t.v[1], t.v[2]); }
inline V3 tri_b(const TriF& t) { return V3(t.v[3], t.v[4], t.v[5]); }
inline V3 tri_c(const TriF& t) { return V3(t.v[6], t.v[7], t.v[8]); }

inline double box_dist2_point(const float* lo, const float* hi, const V3& p) {
    double d2 = 0.0;
    for (int k = 0; k < 3; ++k) {
        const double pk = p[k];
        double d = 0.0;
        if (pk < lo[k]) d = lo[k] - pk;
        else if (pk > hi[k]) d = pk - hi[k];
        d2 += d * d;
    }
    return d2;
}

// Does the segment a + t*d (t in [0,1]) meet the box [lo - r, hi + r]? (slab test)
inline bool seg_hits_box(const V3& a, const V3& d, const float* lo, const float* hi, double r) {
    double t0 = 0.0, t1 = 1.0;
    for (int k = 0; k < 3; ++k) {
        const double ak = a[k], dk = d[k];
        const double l = lo[k] - r, h = hi[k] + r;
        if (std::abs(dk) < 1e-15) {
            if (ak < l || ak > h) return false;
        } else {
            double ta = (l - ak) / dk, tb = (h - ak) / dk;
            if (ta > tb) std::swap(ta, tb);
            if (ta > t0) t0 = ta;
            if (tb < t1) t1 = tb;
            if (t0 > t1) return false;
        }
    }
    return true;
}

inline bool ray_hits_box(const V3& o, const V3& dir, const float* lo, const float* hi) {
    double t0 = 0.0, t1 = kInf;
    for (int k = 0; k < 3; ++k) {
        const double ok = o[k], dk = dir[k];
        if (std::abs(dk) < 1e-15) {
            if (ok < lo[k] || ok > hi[k]) return false;
        } else {
            double ta = (lo[k] - ok) / dk, tb = (hi[k] - ok) / dk;
            if (ta > tb) std::swap(ta, tb);
            if (ta > t0) t0 = ta;
            if (tb < t1) t1 = tb;
            if (t0 > t1) return false;
        }
    }
    return true;
}

// Moller-Trumbore ray/triangle test for t > 0.
inline bool ray_tri(const V3& o, const V3& dir, const TriF& t) {
    const V3 a = tri_a(t), b = tri_b(t), c = tri_c(t);
    const V3 e1 = b - a, e2 = c - a;
    const V3 pvec = cross(dir, e2);
    const double det = dot(e1, pvec);
    if (std::abs(det) < 1e-14) return false;
    const double inv = 1.0 / det;
    const V3 tvec = o - a;
    const double u = dot(tvec, pvec) * inv;
    if (u < 0.0 || u > 1.0) return false;
    const V3 qvec = cross(tvec, e1);
    const double v = dot(dir, qvec) * inv;
    if (v < 0.0 || u + v > 1.0) return false;
    return dot(e2, qvec) * inv > 1e-9;
}

}  // namespace

// ---- triangle / segment distance ------------------------------------------------------

V3 closest_point_on_triangle(const V3& p, const V3& a, const V3& b, const V3& c) {
    const V3 ab = b - a, ac = c - a, ap = p - a;
    const double d1 = dot(ab, ap), d2 = dot(ac, ap);
    if (d1 <= 0.0 && d2 <= 0.0) return a;
    const V3 bp = p - b;
    const double d3 = dot(ab, bp), d4 = dot(ac, bp);
    if (d3 >= 0.0 && d4 <= d3) return b;
    const double vc = d1 * d4 - d3 * d2;
    if (vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0) {
        const double v = d1 / (d1 - d3);
        return a + ab * v;
    }
    const V3 cp = p - c;
    const double d5 = dot(ab, cp), d6 = dot(ac, cp);
    if (d6 >= 0.0 && d5 <= d6) return c;
    const double vb = d5 * d2 - d1 * d6;
    if (vb <= 0.0 && d2 >= 0.0 && d6 <= 0.0) {
        const double w = d2 / (d2 - d6);
        return a + ac * w;
    }
    const double va = d3 * d6 - d5 * d4;
    if (va <= 0.0 && (d4 - d3) >= 0.0 && (d5 - d6) >= 0.0) {
        const double w = (d4 - d3) / ((d4 - d3) + (d5 - d6));
        return b + (c - b) * w;
    }
    const double sum = va + vb + vc;
    if (!(std::abs(sum) > 1e-300)) return a;  // degenerate (zero-area) triangle
    const double denom = 1.0 / sum;
    const double v = vb * denom, w = vc * denom;
    return a + ab * v + ac * w;
}

double point_tri_dist2(const V3& p, const V3& a, const V3& b, const V3& c) {
    return norm2(p - closest_point_on_triangle(p, a, b, c));
}

double seg_seg_closest(const V3& p1, const V3& q1, const V3& p2, const V3& q2, V3* c1o,
                       V3* c2o) {
    const V3 d1 = q1 - p1, d2 = q2 - p2, r = p1 - p2;
    const double a = dot(d1, d1), e = dot(d2, d2), f = dot(d2, r);
    constexpr double eps = 1e-12;
    double s = 0.0, t = 0.0;
    if (a <= eps && e <= eps) {
        s = t = 0.0;
    } else if (a <= eps) {
        s = 0.0;
        t = clamp01(f / e);
    } else {
        const double c = dot(d1, r);
        if (e <= eps) {
            t = 0.0;
            s = clamp01(-c / a);
        } else {
            const double b = dot(d1, d2);
            const double denom = a * e - b * b;
            s = denom > 1e-12 * a * e ? clamp01((b * f - c * e) / denom) : 0.0;
            t = (b * s + f) / e;
            if (t < 0.0) {
                t = 0.0;
                s = clamp01(-c / a);
            } else if (t > 1.0) {
                t = 1.0;
                s = clamp01((b - c) / a);
            }
        }
    }
    const V3 c1 = p1 + d1 * s;
    const V3 c2 = p2 + d2 * t;
    if (c1o) *c1o = c1;
    if (c2o) *c2o = c2;
    return norm2(c1 - c2);
}

bool seg_tri_intersect(const V3& p, const V3& q, const V3& a, const V3& b, const V3& c,
                       V3* hit) {
    const V3 e1 = b - a, e2 = c - a;
    const V3 n = cross(e1, e2);
    const V3 d = q - p;
    const double denom = dot(n, d);
    const double scale = norm(n) * norm(d);
    if (!(std::abs(denom) > 1e-12 * scale) || scale <= 0.0) return false;  // parallel
    const double t = dot(n, a - p) / denom;
    if (t < 0.0 || t > 1.0) return false;
    const V3 h = p + d * t;
    const V3 hv = h - a;
    const double d00 = dot(e1, e1), d01 = dot(e1, e2), d11 = dot(e2, e2);
    const double d20 = dot(hv, e1), d21 = dot(hv, e2);
    const double den = d00 * d11 - d01 * d01;
    if (!(den > 0.0)) return false;
    const double v = (d11 * d20 - d01 * d21) / den;
    const double w = (d00 * d21 - d01 * d20) / den;
    constexpr double eps = 1e-9;
    if (v < -eps || w < -eps || v + w > 1.0 + eps) return false;
    if (hit) *hit = h;
    return true;
}

double seg_tri_closest(const V3& p, const V3& q, const V3& a, const V3& b, const V3& c,
                       V3* on_seg, V3* on_tri) {
    V3 h;
    if (seg_tri_intersect(p, q, a, b, c, &h)) {
        if (on_seg) *on_seg = h;
        if (on_tri) *on_tri = h;
        return 0.0;
    }
    double best = kInf;
    V3 bs, bt;
    auto consider = [&](double d2, const V3& s, const V3& t) {
        if (d2 < best) {
            best = d2;
            bs = s;
            bt = t;
        }
    };
    {
        const V3 ct = closest_point_on_triangle(p, a, b, c);
        consider(norm2(ct - p), p, ct);
    }
    {
        const V3 ct = closest_point_on_triangle(q, a, b, c);
        consider(norm2(ct - q), q, ct);
    }
    const V3* edge[3][2] = {{&a, &b}, {&b, &c}, {&c, &a}};
    for (int k = 0; k < 3; ++k) {
        V3 c1, c2;
        const double d2 = seg_seg_closest(p, q, *edge[k][0], *edge[k][1], &c1, &c2);
        consider(d2, c1, c2);
    }
    if (on_seg) *on_seg = bs;
    if (on_tri) *on_tri = bt;
    return best;
}

// ---- BVH -------------------------------------------------------------------------------

void TriBvh::build(std::vector<TriF> tris) {
    tris_ = std::move(tris);
    nodes_.clear();
    lo_ = V3();
    hi_ = V3();
    const size_t n = tris_.size();
    if (n == 0) return;
    std::vector<float> cen(3 * n);
    for (size_t i = 0; i < n; ++i)
        for (int k = 0; k < 3; ++k)
            cen[3 * i + k] = (tris_[i].v[k] + tris_[i].v[3 + k] + tris_[i].v[6 + k]) / 3.0f;
    std::vector<uint32_t> ids(n);
    std::iota(ids.begin(), ids.end(), 0u);
    nodes_.reserve(2 * n / kLeafSize + 2);
    build_range(ids, 0, n, cen);
    std::vector<TriF> sorted;
    sorted.reserve(n);
    for (size_t k = 0; k < n; ++k) sorted.push_back(tris_[ids[k]]);
    tris_.swap(sorted);
    lo_ = V3(nodes_[0].lo[0], nodes_[0].lo[1], nodes_[0].lo[2]);
    hi_ = V3(nodes_[0].hi[0], nodes_[0].hi[1], nodes_[0].hi[2]);
}

int32_t TriBvh::build_range(std::vector<uint32_t>& ids, size_t lo, size_t hi,
                            const std::vector<float>& cen) {
    const int32_t me = static_cast<int32_t>(nodes_.size());
    nodes_.emplace_back();
    Node node{};
    float mn[3] = {std::numeric_limits<float>::infinity(), std::numeric_limits<float>::infinity(),
                   std::numeric_limits<float>::infinity()};
    float mx[3] = {-std::numeric_limits<float>::infinity(),
                   -std::numeric_limits<float>::infinity(),
                   -std::numeric_limits<float>::infinity()};
    float cmn[3] = {mn[0], mn[1], mn[2]};
    float cmx[3] = {mx[0], mx[1], mx[2]};
    for (size_t k = lo; k < hi; ++k) {
        const TriF& t = tris_[ids[k]];
        for (int v = 0; v < 3; ++v)
            for (int a = 0; a < 3; ++a) {
                mn[a] = std::min(mn[a], t.v[3 * v + a]);
                mx[a] = std::max(mx[a], t.v[3 * v + a]);
            }
        for (int a = 0; a < 3; ++a) {
            cmn[a] = std::min(cmn[a], cen[3 * ids[k] + a]);
            cmx[a] = std::max(cmx[a], cen[3 * ids[k] + a]);
        }
    }
    for (int a = 0; a < 3; ++a) {
        node.lo[a] = mn[a];
        node.hi[a] = mx[a];
    }
    if (hi - lo <= kLeafSize) {
        node.first = static_cast<int32_t>(lo);
        node.count = static_cast<int32_t>(hi - lo);
        nodes_[me] = node;
        return me;
    }
    int axis = 0;
    float ext = cmx[0] - cmn[0];
    for (int a = 1; a < 3; ++a)
        if (cmx[a] - cmn[a] > ext) {
            ext = cmx[a] - cmn[a];
            axis = a;
        }
    const size_t mid = lo + (hi - lo) / 2;
    // Total order (coordinate, then triangle index): the split is deterministic.
    std::nth_element(ids.begin() + static_cast<std::ptrdiff_t>(lo),
                     ids.begin() + static_cast<std::ptrdiff_t>(mid),
                     ids.begin() + static_cast<std::ptrdiff_t>(hi),
                     [&](uint32_t x, uint32_t y) {
                         const float cx = cen[3 * x + axis], cy = cen[3 * y + axis];
                         if (cx != cy) return cx < cy;
                         return x < y;
                     });
    const int32_t left = build_range(ids, lo, mid, cen);
    const int32_t right = build_range(ids, mid, hi, cen);
    node.left = left;
    node.right = right;
    node.first = 0;
    node.count = 0;
    nodes_[me] = node;
    return me;
}

bool TriBvh::any_within_point(const V3& p, const std::vector<double>& thr,
                              double thr_max) const {
    if (nodes_.empty()) return false;
    const double m2 = thr_max * thr_max;
    int32_t stack[kStackSize];
    int sp = 0;
    stack[sp++] = 0;
    while (sp > 0) {
        const Node& nd = nodes_[static_cast<size_t>(stack[--sp])];
        if (box_dist2_point(nd.lo, nd.hi, p) >= m2) continue;
        if (nd.count > 0) {
            for (int32_t i = nd.first; i < nd.first + nd.count; ++i) {
                const TriF& t = tris_[static_cast<size_t>(i)];
                const double th = thr[static_cast<size_t>(t.owner)];
                if (point_tri_dist2(p, tri_a(t), tri_b(t), tri_c(t)) < th * th) return true;
            }
        } else {
            stack[sp++] = nd.left;
            stack[sp++] = nd.right;
        }
    }
    return false;
}

bool TriBvh::any_within_segment(const V3& a, const V3& b, const std::vector<double>& thr,
                                double thr_max) const {
    if (nodes_.empty()) return false;
    const V3 d = b - a;
    int32_t stack[kStackSize];
    int sp = 0;
    stack[sp++] = 0;
    while (sp > 0) {
        const Node& nd = nodes_[static_cast<size_t>(stack[--sp])];
        if (!seg_hits_box(a, d, nd.lo, nd.hi, thr_max)) continue;
        if (nd.count > 0) {
            for (int32_t i = nd.first; i < nd.first + nd.count; ++i) {
                const TriF& t = tris_[static_cast<size_t>(i)];
                const double th = thr[static_cast<size_t>(t.owner)];
                if (seg_tri_closest(a, b, tri_a(t), tri_b(t), tri_c(t), nullptr, nullptr) <
                    th * th)
                    return true;
            }
        } else {
            stack[sp++] = nd.left;
            stack[sp++] = nd.right;
        }
    }
    return false;
}

TriBvh::Nearest TriBvh::nearest_point(const V3& p, double limit) const {
    Nearest best;
    if (nodes_.empty()) return best;
    double best2 = std::isfinite(limit) ? limit * limit : kInf;
    int32_t stack[kStackSize];
    int sp = 0;
    stack[sp++] = 0;
    while (sp > 0) {
        const Node& nd = nodes_[static_cast<size_t>(stack[--sp])];
        if (box_dist2_point(nd.lo, nd.hi, p) >= best2) continue;
        if (nd.count > 0) {
            for (int32_t i = nd.first; i < nd.first + nd.count; ++i) {
                const TriF& t = tris_[static_cast<size_t>(i)];
                const V3 ct = closest_point_on_triangle(p, tri_a(t), tri_b(t), tri_c(t));
                const double d2 = norm2(ct - p);
                if (d2 < best2) {
                    best2 = d2;
                    best.owner = t.owner;
                    best.on_query = p;
                    best.on_tri = ct;
                }
            }
        } else {
            const Node& l = nodes_[static_cast<size_t>(nd.left)];
            const Node& r = nodes_[static_cast<size_t>(nd.right)];
            const double dl = box_dist2_point(l.lo, l.hi, p);
            const double dr = box_dist2_point(r.lo, r.hi, p);
            if (dl <= dr) {  // push the farther child first so the nearer one pops first
                stack[sp++] = nd.right;
                stack[sp++] = nd.left;
            } else {
                stack[sp++] = nd.left;
                stack[sp++] = nd.right;
            }
        }
    }
    if (best.owner >= 0) best.dist = std::sqrt(best2);
    return best;
}

TriBvh::Nearest TriBvh::nearest_segment(const V3& a, const V3& b, double limit) const {
    Nearest best;
    if (nodes_.empty()) return best;
    const V3 d = b - a;
    const V3 mid = lerp(a, b, 0.5);
    double best2 = std::isfinite(limit) ? limit * limit : kInf;
    int32_t stack[kStackSize];
    int sp = 0;
    stack[sp++] = 0;
    while (sp > 0) {
        const Node& nd = nodes_[static_cast<size_t>(stack[--sp])];
        if (std::isfinite(best2) && !seg_hits_box(a, d, nd.lo, nd.hi, std::sqrt(best2)))
            continue;
        if (nd.count > 0) {
            for (int32_t i = nd.first; i < nd.first + nd.count; ++i) {
                const TriF& t = tris_[static_cast<size_t>(i)];
                V3 on_seg, on_tri;
                const double d2 =
                    seg_tri_closest(a, b, tri_a(t), tri_b(t), tri_c(t), &on_seg, &on_tri);
                if (d2 < best2) {
                    best2 = d2;
                    best.owner = t.owner;
                    best.on_query = on_seg;
                    best.on_tri = on_tri;
                }
            }
        } else {
            const Node& l = nodes_[static_cast<size_t>(nd.left)];
            const Node& r = nodes_[static_cast<size_t>(nd.right)];
            const double dl = box_dist2_point(l.lo, l.hi, mid);
            const double dr = box_dist2_point(r.lo, r.hi, mid);
            if (dl <= dr) {
                stack[sp++] = nd.right;
                stack[sp++] = nd.left;
            } else {
                stack[sp++] = nd.left;
                stack[sp++] = nd.right;
            }
        }
    }
    if (best.owner >= 0) best.dist = std::sqrt(best2);
    return best;
}

TriBvh::Nearest TriBvh::first_within_segment(const V3& a, const V3& b,
                                              const std::vector<double>& thr,
                                              double thr_max) const {
    Nearest result;
    if (nodes_.empty()) return result;
    const V3 d = b - a;
    int32_t stack[kStackSize];
    int sp = 0;
    stack[sp++] = 0;
    while (sp > 0) {
        const Node& nd = nodes_[static_cast<size_t>(stack[--sp])];
        if (!seg_hits_box(a, d, nd.lo, nd.hi, thr_max)) continue;
        if (nd.count > 0) {
            for (int32_t i = nd.first; i < nd.first + nd.count; ++i) {
                const TriF& t = tris_[static_cast<size_t>(i)];
                V3 on_seg, on_tri;
                const double d2 = seg_tri_closest(a, b, tri_a(t), tri_b(t), tri_c(t),
                                                  &on_seg, &on_tri);
                const double threshold = thr[static_cast<size_t>(t.owner)];
                if (d2 < threshold * threshold) {
                    result.owner = t.owner;
                    result.dist = std::sqrt(d2);
                    result.on_query = on_seg;
                    result.on_tri = on_tri;
                    return result;
                }
            }
        } else {
            stack[sp++] = nd.left;
            stack[sp++] = nd.right;
        }
    }
    return result;
}

int TriBvh::first_containing_owner(const V3& p, int n_owners) const {
    if (nodes_.empty() || n_owners <= 0) return -1;
    // A fixed, deliberately skew direction: grid-aligned query points and box meshes
    // rarely meet an edge exactly, which would count one crossing twice.
    static const V3 kDir = unit(V3(0.81331, 0.51297, 0.27233));
    thread_local std::vector<uint8_t> parity;
    parity.assign(static_cast<size_t>(n_owners), 0);
    int32_t stack[kStackSize];
    int sp = 0;
    stack[sp++] = 0;
    while (sp > 0) {
        const Node& nd = nodes_[static_cast<size_t>(stack[--sp])];
        if (!ray_hits_box(p, kDir, nd.lo, nd.hi)) continue;
        if (nd.count > 0) {
            for (int32_t i = nd.first; i < nd.first + nd.count; ++i) {
                const TriF& t = tris_[static_cast<size_t>(i)];
                if (ray_tri(p, kDir, t)) parity[static_cast<size_t>(t.owner)] ^= 1u;
            }
        } else {
            stack[sp++] = nd.left;
            stack[sp++] = nd.right;
        }
    }
    for (int i = 0; i < n_owners; ++i)
        if (parity[static_cast<size_t>(i)] & 1u) return i;
    return -1;
}

}  // namespace chudware::harness
