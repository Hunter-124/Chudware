// core/sim/pipe_flow.cpp — see pipe_flow.h. Internal-flow D3Q19 LBM over a closed fluid volume.
#include "sim/pipe_flow.h"

#include "sim/flow_run.h"
#include "sim/progress.h"
#include "sim/sim_cancel.h"
#include "util/resource_governor.h"
#include "util/thread_pool.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <functional>
#include <limits>
#include <map>
#include <numeric>
#include <new>
#include <queue>
#include <string>
#include <tuple>
#include <utility>
#include <vector>
#ifdef CHUDWARE_HAVE_OPENMP
#include <omp.h>
#endif

namespace chudware::sim {

double darcy_friction_factor(double reynolds, double eD) {
    if (!std::isfinite(reynolds) || reynolds <= 0.0) return 0.0;
    if (reynolds < 2100.0) return 64.0 / reynolds;
    if (!std::isfinite(eD) || eD < 0.0) eD = 0.0;
    // Clamond (2009) exact Colebrook solution — identical to core/piping/solve.cpp clamond().
    const double X1 = eD * reynolds * 0.1239681863354175460160858261654858382699;
    const double X2 = std::log(reynolds) - 0.7793974884556819406441139701653776731705;
    double F = X2 - 0.2;
    double X1F = X1 + F;
    double X1F1 = 1.0 + X1F;
    double E = (std::log(X1F) - 0.2) / X1F1;
    F = F - (X1F1 + 0.5 * E) * E * X1F / (X1F1 + E * (1.0 + (1.0 / 3.0) * E));
    X1F = X1 + F;
    X1F1 = 1.0 + X1F;
    E = (std::log(X1F) + F - X2) / X1F1;
    const double b = X1F1 + E * (1.0 + (1.0 / 3.0) * E);
    F = b / (b * F - ((X1F1 + 0.5 * E) * E * X1F));
    return 1.325474527619599502640416597148504422899 * (F * F);
}

namespace {

using json = nlohmann::json;

// D3Q19 velocity set (same order as lbm.cpp), weights, opposites.
constexpr int NQ = 19;
constexpr int CX[NQ] = {0, 1, -1, 0, 0, 0, 0, 1, -1, 1, -1, 1, -1, 1, -1, 0, 0, 0, 0};
constexpr int CY[NQ] = {0, 0, 0, 1, -1, 0, 0, 1, -1, -1, 1, 0, 0, 0, 0, 1, -1, 1, -1};
constexpr int CZ[NQ] = {0, 0, 0, 0, 0, 1, -1, 0, 0, 0, 0, 1, -1, -1, 1, 1, -1, -1, 1};
constexpr int OPP[NQ] = {0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15, 18, 17};
constexpr double W[NQ] = {1.0 / 3.0,  1.0 / 18.0, 1.0 / 18.0, 1.0 / 18.0, 1.0 / 18.0,
                          1.0 / 18.0, 1.0 / 18.0, 1.0 / 36.0, 1.0 / 36.0, 1.0 / 36.0,
                          1.0 / 36.0, 1.0 / 36.0, 1.0 / 36.0, 1.0 / 36.0, 1.0 / 36.0,
                          1.0 / 36.0, 1.0 / 36.0, 1.0 / 36.0, 1.0 / 36.0};
constexpr double CS2 = 1.0 / 3.0;
// TRT "magic" parameter: Lambda = (tau+ - 1/2)(tau- - 1/2) = 3/16 puts the half-way
// bounce-back wall exactly mid-link for Poiseuille flow at every viscosity.
constexpr double kTrtMagic = 3.0 / 16.0;
// Each slab owns its destination nodes; global sums retain their serial order.
template <class Body>
void node_for(size_t count, int threads, const Body& body) {
    const int slabs = static_cast<int>(std::min<size_t>(count, threads));
    auto slab = [&](int s) {
        for (size_t i = count * s / slabs; i < count * (s + 1) / slabs; ++i) body(i);
    };
    if (slabs <= 1) {
        for (size_t i = 0; i < count; ++i) body(i);
        return;
    }
#ifdef CHUDWARE_HAVE_OPENMP
#pragma omp parallel for schedule(static) num_threads(slabs)
    for (int s = 0; s < slabs; ++s) slab(s);
#else
    chudware::pool::shared().parallel_for(slabs, slab);
#endif
}


// Lattice choices. tau 0.8 keeps half-way bounce-back accurate and diffusion fast for
// resolvable laminar runs; turbulent runs use the physical Re with the RANS eddy viscosity.
constexpr double kTauPreferred = 0.8;
constexpr double kUMax = 0.1;            // mean lattice speed cap (Ma ~ 0.17)
constexpr double kUNodeMax = 0.15;       // peak inlet-node speed cap
constexpr double kDrhoBudget = 0.03;     // target lattice pressure drop (cs^2 * drho)
constexpr int kDefaultCellsAcross = 20;
constexpr int kTurbulentCellsAcross = 12;  // wall-function default (validated 10..20)
// Turbulent default when the path bends: a 90-degree LR bend's K comes out 1.47-1.85x Rennels
// at 14-16 cells across vs 2.0-2.3x at 12 (core/tests/test_sim.cpp bend validation).
constexpr int kBendCellsAcross = 16;
// Straight end legs (diameters) a piping run's domain is extended to: developed inflow, and
// the bend wake clear of the constant-pressure outlet so dp includes downstream recovery.
constexpr double kEndLegDiameters = 5.0;
constexpr double kDefaultMaxFluidCells = 3.0e6;
constexpr double kMaxDenseCells = 64.0e6;
constexpr double kBytesPerFluidCell = 456.0;  // 2x19 f64 + 19 i32 nbr + macros + dijkstra
constexpr double kBytesPerDenseCell = 11.0;   // state + node map + layer/profile scratch

struct V3 {
    double x = 0.0, y = 0.0, z = 0.0;
};
inline V3 operator+(const V3& a, const V3& b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline V3 operator-(const V3& a, const V3& b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline V3 operator*(const V3& a, double s) { return {a.x * s, a.y * s, a.z * s}; }
inline double dot(const V3& a, const V3& b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline V3 cross(const V3& a, const V3& b) {
    return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
inline double norm(const V3& a) { return std::sqrt(dot(a, a)); }
inline double comp(const V3& a, int k) { return k == 0 ? a.x : (k == 1 ? a.y : a.z); }

json refuse(const std::string& reason, const std::string& hint) {
    return json{{"ok", false}, {"reason", reason}, {"hint", hint}};
}

// Measured over-prediction of a 90-degree long-radius bend's K (model / Rennels, Re 5e4,
// outlet leg 3-5 D) at a given number of voxels across the hydraulic diameter.
struct BendBand {
    double lo, hi;
    const char* measured_at;
};
BendBand bend_k_band(double cells) {
    if (cells >= 15.0) return {1.47, 1.78, "16"};
    if (cells >= 13.0) return {1.65, 1.85, "14"};
    if (cells >= 11.0) return {2.0, 2.3, "12"};
    return {2.0, 2.4, "10-12"};
}

std::string fmt(const char* f, double a) {
    char buf[64];
    std::snprintf(buf, sizeof buf, f, a);
    return buf;
}

std::string vec_str(const V3& v) {
    char buf[96];
    std::snprintf(buf, sizeof buf, "[%.3g, %.3g, %.3g]", v.x, v.y, v.z);
    return buf;
}

bool parse_vec3(const json& j, V3& out) {
    if (!j.is_array() || j.size() != 3) return false;
    for (const auto& e : j)
        if (!e.is_number()) return false;
    out = {j[0].get<double>(), j[1].get<double>(), j[2].get<double>()};
    return std::isfinite(out.x) && std::isfinite(out.y) && std::isfinite(out.z);
}

struct Plane {
    V3 p, n;  // n unit, oriented into the fluid once resolved
};

bool parse_plane(const json& params, const char* key, Plane& out) {
    if (!params.contains(key) || !params[key].is_object()) return false;
    const json& j = params[key];
    if (!j.contains("point") || !j.contains("normal")) return false;
    if (!parse_vec3(j["point"], out.p) || !parse_vec3(j["normal"], out.n)) return false;
    const double l = norm(out.n);
    if (!(l > 1e-12)) return false;
    out.n = out.n * (1.0 / l);
    return true;
}

// Drop zero-area facets and check the shell is a closed two-manifold (every undirected edge
// shared by exactly two facets, vertices matched by exact coordinates so STL soups work).
// Returns the number of non-manifold edges (0 = watertight); `idx_out` gets the kept facets.
size_t clean_shell(const PipeFlowShell& s, std::vector<uint32_t>& idx_out) {
    const auto& p = s.pos;
    idx_out.clear();
    idx_out.reserve(s.idx.size());
    for (size_t t = 0; t + 2 < s.idx.size(); t += 3) {
        const size_t ia = (size_t)s.idx[t] * 3, ib = (size_t)s.idx[t + 1] * 3,
                     ic = (size_t)s.idx[t + 2] * 3;
        const V3 a{p[ia], p[ia + 1], p[ia + 2]}, b{p[ib], p[ib + 1], p[ib + 2]},
            c{p[ic], p[ic + 1], p[ic + 2]};
        const V3 n = cross(b - a, c - a);
        if (dot(n, n) <= 1e-24) continue;
        idx_out.push_back(s.idx[t]);
        idx_out.push_back(s.idx[t + 1]);
        idx_out.push_back(s.idx[t + 2]);
    }
    if (idx_out.empty()) return 1;
    using Key = std::array<uint32_t, 3>;
    auto key = [&](uint32_t vi) {
        Key k{};
        for (int a = 0; a < 3; ++a) {
            float v = p[(size_t)vi * 3 + a];
            if (v == 0.0f) v = 0.0f;  // canonicalize -0
            std::memcpy(&k[a], &v, sizeof v);
        }
        return k;
    };
    std::map<std::pair<Key, Key>, int> edges;
    for (size_t t = 0; t < idx_out.size(); t += 3)
        for (int e = 0; e < 3; ++e) {
            Key a = key(idx_out[t + e]), b = key(idx_out[t + (e + 1) % 3]);
            if (b < a) std::swap(a, b);
            ++edges[{a, b}];
        }
    size_t bad = 0;
    for (const auto& e : edges)
        if (e.second != 2) ++bad;
    return bad;
}

// Lengthen a straight end leg: the planar end cap lying on plane (p, n) — the connected set of
// coplanar facets around p — moves `len` mm outward (away from the fluid) and a wall strip
// joins its rim to the moved cap. Works on indexed meshes and STL soups (vertices matched by
// exact coordinates, as clean_shell does). On success `p` moves with the cap and true is
// returned; false when no cap facets sit on the plane (the shell is left untouched).
bool extrude_cap(std::vector<PipeFlowShell>& shells, V3& p, const V3& n, double len, double tol) {
    using Key = std::array<uint32_t, 3>;
    auto vkey = [](const float* q) {
        Key k{};
        for (int a = 0; a < 3; ++a) {
            float v = q[a];
            if (v == 0.0f) v = 0.0f;
            std::memcpy(&k[a], &v, sizeof v);
        }
        return k;
    };
    for (auto& s : shells) {
        const size_t nt = s.idx.size() / 3;
        auto P = [&](uint32_t vi) {
            return V3{s.pos[(size_t)vi * 3], s.pos[(size_t)vi * 3 + 1], s.pos[(size_t)vi * 3 + 2]};
        };
        // Coplanar facets (every corner on the plane, facet normal along it).
        std::vector<char> onp(nt, 0);
        size_t seed = SIZE_MAX;
        double best = 1e300;
        for (size_t t = 0; t < nt; ++t) {
            const V3 a = P(s.idx[3 * t]), b = P(s.idx[3 * t + 1]), c = P(s.idx[3 * t + 2]);
            if (std::abs(dot(a - p, n)) > tol || std::abs(dot(b - p, n)) > tol ||
                std::abs(dot(c - p, n)) > tol)
                continue;
            const V3 fn = cross(b - a, c - a);
            const double l = norm(fn);
            if (!(l > 0.0) || std::abs(dot(fn, n)) < 0.999 * l) continue;
            onp[t] = 1;
            const double d = norm((a + b + c) * (1.0 / 3.0) - p);
            if (d < best) best = d, seed = t;
        }
        if (seed == SIZE_MAX) continue;
        // The cap is the edge-connected coplanar patch containing the facet nearest p.
        std::map<std::pair<Key, Key>, std::vector<size_t>> by_edge;
        for (size_t t = 0; t < nt; ++t)
            if (onp[t])
                for (int e = 0; e < 3; ++e) {
                    Key a = vkey(&s.pos[(size_t)s.idx[3 * t + e] * 3]),
                        b = vkey(&s.pos[(size_t)s.idx[3 * t + (e + 1) % 3] * 3]);
                    if (b < a) std::swap(a, b);
                    by_edge[{a, b}].push_back(t);
                }
        std::vector<char> cap(nt, 0);
        std::vector<size_t> stack{seed};
        cap[seed] = 1;
        while (!stack.empty()) {
            const size_t t = stack.back();
            stack.pop_back();
            for (int e = 0; e < 3; ++e) {
                Key a = vkey(&s.pos[(size_t)s.idx[3 * t + e] * 3]),
                    b = vkey(&s.pos[(size_t)s.idx[3 * t + (e + 1) % 3] * 3]);
                if (b < a) std::swap(a, b);
                for (size_t u : by_edge[{a, b}])
                    if (!cap[u]) cap[u] = 1, stack.push_back(u);
            }
        }
        // Outward = away from the bulk of the shell (the fluid side of the plane).
        double side = 0.0;
        for (size_t i = 0; i + 2 < s.pos.size(); i += 3)
            side += dot(V3{s.pos[i], s.pos[i + 1], s.pos[i + 2]} - p, n);
        const V3 out = side > 0.0 ? n * -1.0 : n;
        // Rim edges (used once by the cap) in cap winding, then the moved cap + wall strip.
        std::map<std::pair<Key, Key>, int> use;
        for (size_t t = 0; t < nt; ++t)
            if (cap[t])
                for (int e = 0; e < 3; ++e) {
                    Key a = vkey(&s.pos[(size_t)s.idx[3 * t + e] * 3]),
                        b = vkey(&s.pos[(size_t)s.idx[3 * t + (e + 1) % 3] * 3]);
                    if (b < a) std::swap(a, b);
                    ++use[{a, b}];
                }
        std::map<Key, uint32_t> moved;
        auto moved_of = [&](uint32_t vi) {
            const Key k = vkey(&s.pos[(size_t)vi * 3]);
            auto it = moved.find(k);
            if (it != moved.end()) return it->second;
            const V3 q = P(vi) + out * len;
            const uint32_t ni = (uint32_t)(s.pos.size() / 3);
            s.pos.push_back((float)q.x);
            s.pos.push_back((float)q.y);
            s.pos.push_back((float)q.z);
            moved.emplace(k, ni);
            return ni;
        };
        std::vector<uint32_t> strip;
        for (size_t t = 0; t < nt; ++t) {
            if (!cap[t]) continue;
            const uint32_t v0 = s.idx[3 * t], v1 = s.idx[3 * t + 1], v2 = s.idx[3 * t + 2];
            const uint32_t tv[3] = {v0, v1, v2};
            for (int e = 0; e < 3; ++e) {
                const uint32_t u = tv[e], w = tv[(e + 1) % 3];
                Key a = vkey(&s.pos[(size_t)u * 3]), b = vkey(&s.pos[(size_t)w * 3]);
                if (b < a) std::swap(a, b);
                if (use[{a, b}] != 1) continue;
                const uint32_t u2 = moved_of(u), w2 = moved_of(w);
                strip.insert(strip.end(), {u, w, w2, u, w2, u2});
            }
            s.idx[3 * t] = moved_of(v0);
            s.idx[3 * t + 1] = moved_of(v1);
            s.idx[3 * t + 2] = moved_of(v2);
        }
        s.idx.insert(s.idx.end(), strip.begin(), strip.end());
        p = p + out * len;
        return true;
    }
    return false;
}

struct Grid {
    int nx = 0, ny = 0, nz = 0;
    double v = 1.0;
    V3 o;
    size_t size() const { return (size_t)nx * ny * nz; }
    size_t at(int x, int y, int z) const { return ((size_t)x * ny + y) * nz + z; }
    void coords(size_t c, int& x, int& y, int& z) const {
        z = (int)(c % (size_t)nz);
        y = (int)((c / (size_t)nz) % (size_t)ny);
        x = (int)(c / ((size_t)ny * nz));
    }
    V3 center(size_t c) const {
        int x, y, z;
        coords(c, x, y, z);
        return {o.x + (x + 0.5) * v, o.y + (y + 0.5) * v, o.z + (z + 0.5) * v};
    }
    // Neighbour cell index (c + dir), or SIZE_MAX outside the grid.
    size_t step(size_t c, int dx, int dy, int dz) const {
        int x, y, z;
        coords(c, x, y, z);
        x += dx;
        y += dy;
        z += dz;
        if (x < 0 || y < 0 || z < 0 || x >= nx || y >= ny || z >= nz) return SIZE_MAX;
        return at(x, y, z);
    }
};

// Cell states.
constexpr uint8_t kSolid = 0, kFluid = 1, kInlet = 2, kOutlet = 3, kInterior = 4;

// Orient `pl.n` toward the fluid near pl.p. Returns the distance (mm) from pl.p to the nearest
// fluid cell center, or +inf when there is no fluid at all.
double orient_plane(const Grid& g, const std::vector<size_t>& fluid, Plane& pl) {
    double best = std::numeric_limits<double>::infinity();
    for (size_t c : fluid) best = std::min(best, norm(g.center(c) - pl.p));
    if (!std::isfinite(best)) return best;
    const double r = best + 1.5 * g.v;
    size_t pos = 0, neg = 0;
    for (size_t c : fluid) {
        const V3 d = g.center(c) - pl.p;
        if (norm(d) > r) continue;
        const double s = dot(d, pl.n);
        if (s > 1e-9 * g.v) ++pos;
        else if (s < -1e-9 * g.v) ++neg;
    }
    if (neg > 0 && pos == 0) pl.n = pl.n * -1.0;
    return best;
}

// The boundary layer for a plane: the one-voxel-thick slab of fluid cells nearest the plane
// on its fluid side (centers within half a voxel of the plane when the plane cuts through the
// volume; the first cell layer inside an end cap that does not sit on a cell face), restricted
// to the 26-connected component nearest the plane point so a coplanar far-away crossing of
// another leg is never picked up.
std::vector<size_t> plane_layer(const Grid& g, const std::vector<uint8_t>& state,
                                const std::vector<size_t>& fluid, const Plane& pl,
                                double seed_limit) {
    const double eps = 1e-9 * g.v;
    double d_near = std::numeric_limits<double>::infinity();
    for (size_t c : fluid) {
        if (state[c] != kFluid) continue;
        const V3 ctr = g.center(c);
        const double d = dot(ctr - pl.p, pl.n);
        if (d > -0.5 * g.v + eps && norm(ctr - pl.p) <= seed_limit) d_near = std::min(d_near, d);
    }
    std::vector<size_t> layer;
    if (!std::isfinite(d_near)) return layer;
    std::vector<uint8_t> cand(g.size(), 0);
    size_t seed = SIZE_MAX;
    double seed_dist = std::numeric_limits<double>::infinity();
    for (size_t c : fluid) {
        if (state[c] != kFluid) continue;
        const V3 ctr = g.center(c);
        const double d = dot(ctr - pl.p, pl.n);
        if (d >= d_near - eps && d < d_near + g.v - eps) {
            cand[c] = 1;
            const double r = norm(ctr - pl.p);
            if (r < seed_dist) {
                seed_dist = r;
                seed = c;
            }
        }
    }
    if (seed == SIZE_MAX || seed_dist > seed_limit) return layer;
    std::vector<size_t> stack{seed};
    cand[seed] = 2;
    while (!stack.empty()) {
        const size_t c = stack.back();
        stack.pop_back();
        layer.push_back(c);
        for (int dx = -1; dx <= 1; ++dx)
            for (int dy = -1; dy <= 1; ++dy)
                for (int dz = -1; dz <= 1; ++dz) {
                    if (!dx && !dy && !dz) continue;
                    const size_t nb = g.step(c, dx, dy, dz);
                    if (nb == SIZE_MAX || cand[nb] != 1) continue;
                    cand[nb] = 2;
                    stack.push_back(nb);
                }
    }
    std::sort(layer.begin(), layer.end());
    return layer;
}

// Plane section of the shells: area and wetted perimeter of the cut, restricted to segments
// inside `lo..hi` (the inlet layer's box) so another leg crossing the same plane is excluded.
// Holes subtract through the consistent segment orientation (n x facet normal).
struct Section {
    double area_mm2 = 0.0, perimeter_mm = 0.0;
};
Section slice_section(const std::vector<PipeFlowShell>& shells,
                      const std::vector<std::vector<uint32_t>>& idx, const Plane& pl,
                      const V3& lo, const V3& hi) {
    Section s;
    double signed_area = 0.0;
    auto inside = [&](const V3& q) {
        return q.x >= lo.x && q.y >= lo.y && q.z >= lo.z && q.x <= hi.x && q.y <= hi.y &&
               q.z <= hi.z;
    };
    for (size_t b = 0; b < shells.size(); ++b) {
        const auto& p = shells[b].pos;
        const auto& id = idx[b];
        for (size_t t = 0; t + 2 < id.size(); t += 3) {
            V3 P[3];
            double d[3];
            for (int k = 0; k < 3; ++k) {
                const size_t o = (size_t)id[t + k] * 3;
                P[k] = {p[o], p[o + 1], p[o + 2]};
                d[k] = dot(P[k] - pl.p, pl.n);
            }
            V3 pts[2];
            int np = 0;
            for (int e = 0; e < 3 && np < 2; ++e) {
                int i = e, j = (e + 1) % 3;
                if ((d[i] >= 0.0) == (d[j] >= 0.0)) continue;
                // Canonical edge order so a shared edge yields bit-identical points.
                if (std::make_tuple(P[j].x, P[j].y, P[j].z) <
                    std::make_tuple(P[i].x, P[i].y, P[i].z))
                    std::swap(i, j);
                const double tt = d[i] / (d[i] - d[j]);
                pts[np++] = P[i] + (P[j] - P[i]) * tt;
            }
            if (np != 2 || !inside(pts[0]) || !inside(pts[1])) continue;
            const V3 dir = cross(pl.n, cross(P[1] - P[0], P[2] - P[0]));
            V3 a = pts[0], c = pts[1];
            if (dot(c - a, dir) < 0.0) std::swap(a, c);
            s.perimeter_mm += norm(c - a);
            signed_area += 0.5 * dot(pl.n, cross(a - pl.p, c - pl.p));
        }
    }
    s.area_mm2 = std::abs(signed_area);
    return s;
}

// Developed laminar inlet profile: solve -lap(u) = 1 on the inlet layer (in-plane 4-neighbour
// stencil across the two axes other than the normal's dominant axis) with half-way no-slip
// ghosts at solid neighbours; fluid non-inlet neighbours are zero-flux. Returns the profile
// scaled to unit mean, or empty when the system is singular (no wall touches the layer).
std::vector<double> developed_profile(const Grid& g, const std::vector<uint8_t>& state,
                                      const std::vector<size_t>& layer,
                                      const std::vector<int32_t>& local_of, int axis) {
    const size_t n = layer.size();
    const int a1 = (axis + 1) % 3, a2 = (axis + 2) % 3;
    std::vector<std::array<int32_t, 4>> nb(n);
    std::vector<double> diag(n, 0.0);
    bool any_wall = false;
    for (size_t i = 0; i < n; ++i) {
        int k = 0;
        for (int ax : {a1, a2})
            for (int sgn : {-1, 1}) {
                const int dx = ax == 0 ? sgn : 0, dy = ax == 1 ? sgn : 0, dz = ax == 2 ? sgn : 0;
                const size_t c = g.step(layer[i], dx, dy, dz);
                int32_t j = -1;
                if (c == SIZE_MAX || state[c] == kSolid) {
                    diag[i] += 2.0;  // ghost -u at the half-way wall
                    any_wall = true;
                } else if (state[c] == kInlet && local_of[c] >= 0) {
                    diag[i] += 1.0;
                    j = local_of[c];
                }
                nb[i][k++] = j;
            }
    }
    if (!any_wall) return {};
    auto apply = [&](const std::vector<double>& x, std::vector<double>& y) {
        for (size_t i = 0; i < n; ++i) {
            double s = diag[i] * x[i];
            for (int32_t j : nb[i])
                if (j >= 0) s -= x[(size_t)j];
            y[i] = s;
        }
    };
    // Jacobi-preconditioned CG on the SPD system.
    std::vector<double> x(n, 0.0), r(n, 1.0), z(n), p(n), q(n);
    for (size_t i = 0; i < n; ++i) {
        if (diag[i] <= 0.0) return {};
        z[i] = r[i] / diag[i];
    }
    p = z;
    double rz = 0.0;
    for (size_t i = 0; i < n; ++i) rz += r[i] * z[i];
    const double r0 = std::sqrt((double)n);
    for (size_t it = 0; it < 20 * n + 200; ++it) {
        apply(p, q);
        double pq = 0.0;
        for (size_t i = 0; i < n; ++i) pq += p[i] * q[i];
        if (!(pq > 0.0)) return {};
        const double alpha = rz / pq;
        double rr = 0.0;
        for (size_t i = 0; i < n; ++i) {
            x[i] += alpha * p[i];
            r[i] -= alpha * q[i];
            rr += r[i] * r[i];
        }
        if (std::sqrt(rr) < 1e-11 * r0) break;
        double rz_new = 0.0;
        for (size_t i = 0; i < n; ++i) {
            z[i] = r[i] / diag[i];
            rz_new += r[i] * z[i];
        }
        const double beta = rz_new / rz;
        rz = rz_new;
        for (size_t i = 0; i < n; ++i) p[i] = z[i] + beta * p[i];
    }
    double mean = 0.0;
    for (double val : x) {
        if (!(val > 0.0) || !std::isfinite(val)) return {};
        mean += val;
    }
    mean /= (double)n;
    for (double& val : x) val /= mean;
    return x;
}

inline void feq19(double rho, double ux, double uy, double uz, double* out) {
    // He–Luo incompressible equilibrium (rho0 = 1): w (rho + 3cu + 4.5cu^2 - 1.5u^2).
    const double usq = 1.5 * (ux * ux + uy * uy + uz * uz);
    for (int q = 0; q < NQ; ++q) {
        const double cu = 3.0 * (CX[q] * ux + CY[q] * uy + CZ[q] * uz);
        out[q] = W[q] * (rho + cu + 0.5 * cu * cu - usq);
    }
}

// Turbulence model constants: log law (kappa, B), van Driest damping A+.
constexpr double kKarman = 0.41;
constexpr double kLogB = 5.2;
constexpr double kVanDriestA = 26.0;
constexpr double kMixingCap = 0.09;     // mixing length cap, fraction of D_h/2 (Escudier)
// Turbulent runs oscillate slightly about their steady RANS state; converged when the mean dp
// over the last 4 check windows is within 1% of the 4 before it.
constexpr double kTurbulentTol = 1e-2;
// Lattice wall-stress coefficient of the force-based wall function. Sampling u_tau one cell off
// a staircase bounce-back wall reads the near-wall velocity high (the wall cell itself lags);
// 0.6 brings developed straight-pipe friction to within ~10% of Colebrook/Clamond for Re
// 1e4..1e6 at 10..20 cells across the hydraulic diameter (core/tests/test_sim.cpp).
constexpr double kWallStressCoef = 0.6;

// Spalding's single-formula law of the wall, y+ = u+ + e^{-kB}(e^{ku+} - 1 - ku+ - (ku+)^2/2
// - (ku+)^3/6), solved for u_tau by Newton from `guess` (valid from the viscous sublayer
// through the log layer). ut = wall-parallel speed at distance y; consistent units.
double spalding_utau(double ut, double y, double nu, double guess, int iters) {
    if (!(ut > 0.0) || !(y > 0.0) || !(nu > 0.0)) return 0.0;
    const double E = std::exp(-kKarman * kLogB);
    double u_tau = guess > 0.0 ? guess : std::sqrt(nu * ut / y);
    for (int it = 0; it < iters; ++it) {
        u_tau = std::max(u_tau, ut / 80.0);  // u+ <= 80 keeps exp() finite
        const double up = ut / u_tau;
        const double k = kKarman * up;
        const double e = std::exp(k);
        const double g =
            up + E * (e - 1.0 - k - 0.5 * k * k - k * k * k / 6.0) - y * u_tau / nu;
        const double dg_dup = 1.0 + E * kKarman * (e - 1.0 - k - 0.5 * k * k);
        const double dg = -dg_dup * up / u_tau - y / nu;
        double next = u_tau - g / dg;
        if (!(next > 0.0) || !std::isfinite(next)) next = 0.5 * u_tau;
        const bool done = std::abs(next - u_tau) <= 1e-10 * u_tau;
        u_tau = next;
        if (done) break;
    }
    return u_tau;
}

// 5x5x5 chamfer moves: every primitive step with components in [-2,2]; length-2 steps must
// pass through the two cells straddling their midpoint (lo/hi).
struct Move {
    int d[3], lo[3], hi[3];
    double len;  // lattice units
    bool via;
};
std::vector<Move> chamfer_moves() {
    std::vector<Move> moves;
    for (int a = -2; a <= 2; ++a)
        for (int b = -2; b <= 2; ++b)
            for (int c = -2; c <= 2; ++c) {
                const int m[3] = {std::abs(a), std::abs(b), std::abs(c)};
                if (std::gcd(std::gcd(m[0], m[1]), m[2]) != 1) continue;
                Move mv{{a, b, c}, {}, {}, std::sqrt((double)(a * a + b * b + c * c)),
                        m[0] == 2 || m[1] == 2 || m[2] == 2};
                for (int k = 0; k < 3; ++k) {
                    mv.lo[k] = (int)std::floor(mv.d[k] / 2.0);
                    mv.hi[k] = (int)std::ceil(mv.d[k] / 2.0);
                }
                moves.push_back(mv);
            }
    return moves;
}

struct Lattice {
    size_t n_int = 0, n_in = 0, n_out = 0;  // node blocks: interior | inlet | outlet
    std::vector<size_t> cell;               // node -> grid cell
    std::vector<int32_t> nbr;               // interior: pull source node per q, -1 = wall
    std::vector<int32_t> bc_nb;             // BC node (offset n_int) -> interior neighbour
    std::vector<V3> inlet_u;                // per inlet node: unit-mean profile * normal
    // Boundary links (BC node b, direction q from b, interior node i): the net lattice mass
    // entering the interior per step is G[b][q] - G[i][opp q].
    struct Link {
        int32_t b, q, i;
    };
    std::vector<Link> in_links, out_links;
    // Walls: interior nodes with at least one bounce-back link.
    std::vector<int32_t> wall_of;    // interior node -> wall index, -1 = none
    std::vector<int32_t> wall_node;  // wall index -> interior node
    std::vector<V3> wall_n;          // unit sum of w_q c_q over its wall links (into fluid)
    std::vector<double> wall_y;      // node-to-wall distance, lattice units
    std::vector<double> wall_area;   // projected wall area: |vector sum of the solid faces|
    std::vector<double> y;           // interior node wall distance, lattice units
    std::vector<int32_t> near_wall;  // interior node -> nearest wall index
    // Turbulent recycling inlet: per inlet node, the interior node at the same cross-section
    // position a few diameters down the straight inlet leg (-1 none). Empty = no recycling.
    std::vector<int32_t> recycle;
    V3 inlet_n;  // unit inlet normal (into the fluid)
    size_t nodes() const { return n_int + n_in + n_out; }
};

enum class Collision {
    Trt,          // laminar: TRT at the magic parameter (viscosity-independent walls)
    Regularized,  // laminar near tau 1/2: regularized BGK
    Rans,         // turbulent: regularized BGK + mixing-length eddy viscosity + wall function
};

struct SolveCfg {
    Collision collision = Collision::Trt;
    double u_node = 0.05;  // mean prescribed lattice speed over the inlet nodes
    double nu0 = 0.1;      // molecular lattice viscosity (tau0 = 3 nu0 + 1/2)
    double l_cap = 1.0;    // mixing-length cap, lattice units
    double r_core = 1.0;   // D_h/2 in lattice units (Reichardt core distribution)
    int max_steps = 10000;
    int check_every = 100;
    int ramp_steps = 0;
    double t_relax = 1000.0;  // slowest relaxation-time estimate (steps)
    double tol = 1e-3;
    int threads = 1;
    const std::vector<V3>* u_init = nullptr;        // initial velocity per node (optional)
    const std::vector<double>* rho_init = nullptr;  // initial density per node (optional)
};

struct SolveOut {
    bool diverged = false, cancelled = false, converged = false;
    int steps = 0;
    double q_in = 0.0, q_out = 0.0;  // lattice mass flux per step
    double max_drho = 0.0;
    std::vector<double> rho, ux, uy, uz;  // instantaneous macros (lattice units)
    std::vector<double> rho_mean;         // density averaged over the last check window
    // Wall model diagnostics (Rans): first-cell y+ and friction velocity (lattice units).
    double yplus_mean = 0.0, yplus_min = 0.0, yplus_max = 0.0, utau_mean = 0.0;
    std::vector<double> utau_wall;  // final friction velocity per wall cell (lattice units)
};

double layer_pressure(const std::vector<double>& rho, size_t first, size_t count) {
    double s = 0.0;
    for (size_t k = 0; k < count; ++k) s += rho[first + k];
    return count ? CS2 * (s / (double)count - 1.0) : 0.0;
}

SolveOut solve_lattice(const Lattice& L, const SolveCfg& cfg,
                       const std::function<bool()>& should_cancel,
                       const std::function<void(int, double, double, double)>& on_check) {
    const size_t N = L.nodes();
    SolveOut out;
    std::vector<double> F(N * NQ), G(N * NQ);
    out.rho.assign(N, 1.0);
    out.ux.assign(N, 0.0);
    out.uy.assign(N, 0.0);
    out.uz.assign(N, 0.0);
    auto& rho = out.rho;
    auto& ux = out.ux;
    auto& uy = out.uy;
    auto& uz = out.uz;
    for (size_t i = 0; i < N; ++i) {
        if (cfg.rho_init) rho[i] = (*cfg.rho_init)[i];
        if (cfg.u_init) {
            ux[i] = (*cfg.u_init)[i].x;
            uy[i] = (*cfg.u_init)[i].y;
            uz[i] = (*cfg.u_init)[i].z;
        }
        feq19(rho[i], ux[i], uy[i], uz[i], &F[i * NQ]);
    }
    // Inlet profile (unit-mean shape times the inlet normal). Recycled turbulent runs replace
    // it each window with the downstream cross-section's shape, which converges to the
    // model's own developed profile, so the result carries no artificial entrance loss.
    std::vector<V3> inlet_u = L.inlet_u;
    const double nu0 = cfg.nu0;
    const double tau0 = 3.0 * nu0 + 0.5;
    const bool rans = cfg.collision == Collision::Rans;
    const bool trt = cfg.collision == Collision::Trt;
    const int threads = cfg.threads;

    // Wall model (Rans): a force-based wall function. Every wall cell keeps half-way
    // bounce-back for impermeability, but the tangential momentum the bounce-back exchanges
    // is measured each step and replaced (exact-difference body force on the cell) by the
    // law-of-the-wall stress kWallStressCoef * u_tau^2 over the cell's projected wall area.
    // u_tau comes from Spalding's law at the wall-parallel velocity and wall distance of the
    // cell's reference node — the first fluid cell off the wall along its normal — like a
    // finite-volume wall function sampled one cell out. The pipe's force balance carries the
    // wall stress into the pressure drop at any first-cell y+. The wall cell's own eddy
    // viscosity is the log-layer kappa u_tau y_P.
    const size_t n_wall = rans ? L.wall_node.size() : 0;
    std::vector<double> utau(n_wall, 0.0), tau_wall(n_wall, tau0);
    std::vector<double> lmix(rans ? L.n_int : 0, 0.0), tau_floor(rans ? L.n_int : 0, 0.5);
    std::vector<int32_t> wref(n_wall, -1);
    for (size_t w = 0; w < n_wall; ++w) {
        const size_t P = (size_t)L.wall_node[w];
        const V3& n = L.wall_n[w];
        double best = 0.25;
        for (int q = 1; q < NQ; ++q) {
            const int32_t j = L.nbr[P * NQ + OPP[q]];  // node at P + c_q
            if (j < 0 || (size_t)j >= L.n_int || L.wall_of[(size_t)j] >= 0) continue;
            const double cl = std::sqrt((double)(CX[q] * CX[q] + CY[q] * CY[q] + CZ[q] * CZ[q]));
            const double a = (CX[q] * n.x + CY[q] * n.y + CZ[q] * n.z) / cl;
            if (a > best) {
                best = a;
                wref[w] = j;
            }
        }
    }
    auto update_wall = [&](int iters) {
        node_for(n_wall, threads, [&](size_t w) {
            const size_t P = (size_t)L.wall_node[w];
            const size_t src = wref[w] >= 0 ? (size_t)wref[w] : P;
            const V3 u{ux[src], uy[src], uz[src]};
            const V3& n = L.wall_n[w];
            const double yp = L.wall_y[w];
            const double ys = wref[w] >= 0 ? L.y[src] : yp;
            const double tu = spalding_utau(norm(u - n * dot(u, n)), ys, nu0, utau[w], iters);
            utau[w] = tu;
            tau_wall[w] = 0.5 + 3.0 * (kKarman * tu * yp + nu0);
        });
    };
    // Mixing length l = min(kappa y (1 - e^{-y+/A+}), cap) with the nearest wall's u_tau, and
    // Reichardt's eddy-viscosity distribution nu_t = kappa u_tau R/6 (1 - xi^2)(1 + 2 xi^2),
    // xi = 1 - y/R, as the floor: it matches kappa u_tau y in the log layer and keeps the
    // core viscosity finite where the mixing-length |S| vanishes (pipe axis).
    auto update_mixing = [&]() {
        node_for(L.n_int, threads, [&](size_t i) {
            const int32_t w = L.near_wall[i];
            const double ut = w >= 0 ? utau[(size_t)w] : 0.0;
            const double yp = L.y[i] * ut / nu0;
            const double damp = 1.0 - std::exp(-yp / kVanDriestA);
            lmix[i] = std::min(kKarman * L.y[i] * damp, cfg.l_cap);
            const double xi = 1.0 - std::min(1.0, L.y[i] / cfg.r_core);
            const double nut_r = kKarman * ut * cfg.r_core / 6.0 * (1.0 - xi * xi) *
                                 (1.0 + 2.0 * xi * xi) * damp * damp;
            tau_floor[i] = 0.5 + 3.0 * (nu0 + nut_r);
        });
    };
    if (rans) {
        update_wall(60);
        update_mixing();
    }

    auto fluxes = [&](const std::vector<double>& P, double& qin, double& qout) {
        qin = 0.0;
        qout = 0.0;
        for (const auto& l : L.in_links)
            qin += P[(size_t)l.b * NQ + l.q] - P[(size_t)l.i * NQ + OPP[l.q]];
        for (const auto& l : L.out_links)
            qout += P[(size_t)l.i * NQ + OPP[l.q]] - P[(size_t)l.b * NQ + l.q];
    };

    // Window averages (over each check window) filter the weakly damped acoustic modes out of
    // the convergence test and the reported fields.
    std::vector<double> rho_acc(N, 0.0);
    double qin_acc = 0.0, qout_acc = 0.0;
    int acc_n = 0;
    double prev_dp = std::numeric_limits<double>::quiet_NaN();
    int passes = 0;
    // Heartbeats do not advance or reset the step-based convergence windows.
    constexpr double kHeartbeatS = 1.0;
    auto last_beat = std::chrono::steady_clock::now();
    struct Window {
        std::vector<double> rho;
        double qin, qout, dp;
    };
    std::vector<Window> ring;  // last check windows (turbulent averaging + stationarity)
    constexpr size_t kAvg = 4;
    ring.reserve(2 * kAvg);
    std::vector<double> win_rho(N), recycle_shape(L.recycle.empty() ? 0 : L.n_in);
    if (!rans) out.rho_mean.resize(N);
    for (int step = 1; step <= cfg.max_steps; ++step) {
        // Interior: pull-stream with half-way bounce-back, macros, collision into G (Rans wall
        // cells: bounce-back tangential momentum swapped for the wall-function stress).
        node_for(L.n_int, threads, [&](size_t i) {
            const int32_t wi = rans ? L.wall_of[i] : -1;
            double f[NQ];
            const int32_t* nb = &L.nbr[i * NQ];
            for (int q = 0; q < NQ; ++q) {
                const int32_t s = nb[q];
                f[q] = s >= 0 ? F[(size_t)s * NQ + q] : F[i * NQ + OPP[q]];
            }
            double r = 0.0, jx = 0.0, jy = 0.0, jz = 0.0;
            for (int q = 0; q < NQ; ++q) {
                r += f[q];
                jx += CX[q] * f[q];
                jy += CY[q] * f[q];
                jz += CZ[q] * f[q];
            }
            double fe[NQ];
            feq19(r, jx, jy, jz, fe);
            double* gi = &G[i * NQ];
            if (trt) {
                // Symmetric part relaxes with the viscosity, antisymmetric at the magic rate.
                const double om_p = 1.0 / tau0;
                const double om_m = 1.0 / (kTrtMagic / (tau0 - 0.5) + 0.5);
                for (int q = 0; q < NQ; ++q) {
                    const int o = OPP[q];
                    const double fp = 0.5 * (f[q] + f[o]), fm = 0.5 * (f[q] - f[o]);
                    const double ep = 0.5 * (fe[q] + fe[o]), em = 0.5 * (fe[q] - fe[o]);
                    gi[q] = f[q] - om_p * (fp - ep) - om_m * (fm - em);
                }
            } else {
                double pxx = 0, pyy = 0, pzz = 0, pxy = 0, pxz = 0, pyz = 0;
                for (int q = 1; q < NQ; ++q) {
                    const double ne = f[q] - fe[q];
                    pxx += CX[q] * CX[q] * ne;
                    pyy += CY[q] * CY[q] * ne;
                    pzz += CZ[q] * CZ[q] * ne;
                    pxy += CX[q] * CY[q] * ne;
                    pxz += CX[q] * CZ[q] * ne;
                    pyz += CY[q] * CZ[q] * ne;
                }
                double tau = tau0;
                if (rans) {
                    // nu_t = l^2 |S| with |S| from the non-equilibrium stress (implicit in tau,
                    // solved in closed form like Smagorinsky), floored by Reichardt's profile.
                    const double Q = std::sqrt(2.0 * (pxx * pxx + pyy * pyy + pzz * pzz +
                                                      2.0 * (pxy * pxy + pxz * pxz + pyz * pyz)));
                    const double l = lmix[i];
                    tau = wi >= 0 ? tau_wall[(size_t)wi]
                                  : std::max(tau_floor[i], 0.5 * (tau0 + std::sqrt(tau0 * tau0 +
                                                                                  18.0 * l * l * Q)));
                }
                // Regularized: rebuild the non-equilibrium part from its stress moment only.
                const double keep = 1.0 - 1.0 / tau;
                const double tr3 = (pxx + pyy + pzz) / 3.0;
                for (int q = 0; q < NQ; ++q) {
                    const double cpc = CX[q] * CX[q] * pxx + CY[q] * CY[q] * pyy +
                                       CZ[q] * CZ[q] * pzz +
                                       2.0 * (CX[q] * CY[q] * pxy + CX[q] * CZ[q] * pxz +
                                              CY[q] * CZ[q] * pyz);
                    gi[q] = fe[q] + keep * 4.5 * W[q] * (cpc - tr3);
                }
            }
            if (wi >= 0) {
                // Force-based wall function: swap the bounce-back's tangential momentum
                // exchange (2 f c over the wall links, tangential part) for the calibrated
                // law-of-the-wall stress over the cell's wall area (exact-difference force).
                const size_t w = (size_t)wi;
                V3 fw;
                for (int q = 1; q < NQ; ++q)
                    if (nb[q] < 0)
                        fw = fw + V3{(double)CX[q], (double)CY[q], (double)CZ[q]} * (2.0 * f[q]);
                const V3& n = L.wall_n[w];
                const V3 ft = fw - n * dot(fw, n);
                const V3 uc{jx, jy, jz};
                const V3 t = uc - n * dot(uc, n);
                const double tn = norm(t);
                const V3 fwf =
                    tn > 1e-14 ? t * (-kWallStressCoef * utau[w] * utau[w] * L.wall_area[w] / tn)
                               : V3{};
                const V3 fc = fwf - ft;
                double fa[NQ];
                feq19(r, jx + fc.x, jy + fc.y, jz + fc.z, fa);
                for (int q = 0; q < NQ; ++q) gi[q] += fa[q] - fe[q];
                jx += 0.5 * fc.x;
                jy += 0.5 * fc.y;
                jz += 0.5 * fc.z;
            }
            rho[i] = r;
            rho_acc[i] += r;
            ux[i] = jx;
            uy[i] = jy;
            uz[i] = jz;
        });
        // u_tau drifts on the momentum-diffusion time scale, so a Newton pair every 4 steps
        // tracks it at a quarter of the cost.
        if (rans && step % 4 == 0) update_wall(2);
        // Boundaries: non-equilibrium extrapolation from the interior neighbour. Inlet: the
        // prescribed velocity with the neighbour's density; outlet: rho = 1 with the
        // neighbour's velocity.
        const double ramp =
            (cfg.ramp_steps > 0 && step < cfg.ramp_steps)
                ? 0.5 * (1.0 - std::cos(3.14159265358979323846 * step / cfg.ramp_steps))
                : 1.0;
        const size_t n_bc = L.n_in + L.n_out;
        node_for(n_bc, threads, [&](size_t k) {
            const size_t b = L.n_int + k;
            const int32_t nb = L.bc_nb[k];
            double rb, bx, by, bz;
            if (k < L.n_in) {
                const V3& u = inlet_u[k];
                const double s = cfg.u_node * ramp;
                bx = u.x * s;
                by = u.y * s;
                bz = u.z * s;
                rb = nb >= 0 ? rho[(size_t)nb] : 1.0;
            } else {
                rb = 1.0;
                bx = nb >= 0 ? ux[(size_t)nb] : 0.0;
                by = nb >= 0 ? uy[(size_t)nb] : 0.0;
                bz = nb >= 0 ? uz[(size_t)nb] : 0.0;
            }
            double fb[NQ];
            feq19(rb, bx, by, bz, fb);
            double* gb = &G[b * NQ];
            if (nb >= 0) {
                double fn[NQ];
                const size_t s = (size_t)nb;
                feq19(rho[s], ux[s], uy[s], uz[s], fn);
                const double* gn = &G[s * NQ];
                for (int q = 0; q < NQ; ++q) gb[q] = fb[q] + (gn[q] - fn[q]);
            } else {
                for (int q = 0; q < NQ; ++q) gb[q] = fb[q];
            }
            rho[b] = rb;
            rho_acc[b] += rb;
            ux[b] = bx;
            uy[b] = by;
            uz[b] = bz;
        });
        F.swap(G);
        out.steps = step;
        {
            double qi = 0.0, qo = 0.0;
            fluxes(F, qi, qo);
            qin_acc += qi;
            qout_acc += qo;
            ++acc_n;
        }

        if (should_cancel && should_cancel()) {
            out.cancelled = true;
            return out;
        }
        const bool due_check = step % cfg.check_every == 0 || step == cfg.max_steps;
        const auto now = std::chrono::steady_clock::now();
        if (!due_check && std::chrono::duration<double>(now - last_beat).count() < kHeartbeatS)
            continue;
        last_beat = now;

        double max_drho = 0.0;
        bool finite = true;
        for (size_t i = 0; i < N; ++i) {
            const double d = std::abs(rho[i] - 1.0);
            if (!std::isfinite(d)) {
                finite = false;
                break;
            }
            max_drho = std::max(max_drho, d);
        }
        out.max_drho = max_drho;
        if (!finite || max_drho > 0.5) {
            out.diverged = true;
            return out;
        }
        const double qin_mean = qin_acc / acc_n, qout_mean = qout_acc / acc_n;
        double rin = 0.0;
        for (size_t k = 0; k < L.n_in; ++k) rin += rho_acc[L.n_int + k];
        const double dp = L.n_in ? CS2 * (rin / ((double)L.n_in * acc_n) - 1.0) : 0.0;
        const double imbalance =
            qin_mean > 0.0 ? std::abs(qin_mean - qout_mean) / qin_mean : 1.0;
        if (on_check) on_check(step, dp, imbalance, max_drho);
        if (!due_check) continue;  // heartbeat only: the convergence window is step-based
        if (rans) update_mixing();
        if (!L.recycle.empty() && L.n_in) {
            const V3& n_in = L.inlet_n;
            auto& shape = recycle_shape;
            double mean = 0.0;
            for (size_t k = 0; k < L.n_in; ++k) {
                const int32_t s = L.recycle[k];
                shape[k] = s >= 0 ? std::max(0.0, dot(V3{ux[(size_t)s], uy[(size_t)s], uz[(size_t)s]}, n_in))
                                  : dot(inlet_u[k], n_in) * cfg.u_node;
                mean += shape[k];
            }
            mean /= (double)L.n_in;
            if (mean > 0.0)
                for (size_t k = 0; k < L.n_in; ++k)
                    inlet_u[k] = inlet_u[k] * 0.5 + n_in * (0.5 * shape[k] / mean);
        }
        // Turbulent (RANS) runs settle to a statistically steady state with small persistent
        // oscillations: they converge when the mean of the last kAvg windows matches the mean
        // of the kAvg before it, and report the fields averaged over those last windows.
        // Reuse the expired window's storage; no full-field allocation each check.
        if (rans && ring.size() == 2 * kAvg) {
            win_rho.swap(ring.front().rho);
            ring.erase(ring.begin());
        }
        node_for(N, threads, [&](size_t i) {
            win_rho[i] = rho_acc[i] / acc_n;
            rho_acc[i] = 0.0;
        });
        qin_acc = qout_acc = 0.0;
        acc_n = 0;
        bool stop = false;
        if (rans) {
            ring.push_back({std::move(win_rho), qin_mean, qout_mean, dp});
            if (ring.size() < 2 * kAvg) win_rho.resize(N);
            const size_t nr = std::min(ring.size(), kAvg);
            out.rho_mean.resize(N);
            node_for(N, threads, [&](size_t i) {
                double mean = 0.0;
                for (size_t r_ = ring.size() - nr; r_ < ring.size(); ++r_)
                    mean += ring[r_].rho[i] / nr;
                out.rho_mean[i] = mean;
            });
            out.q_in = out.q_out = 0.0;
            for (size_t r_ = ring.size() - nr; r_ < ring.size(); ++r_) {
                out.q_in += ring[r_].qin / nr;
                out.q_out += ring[r_].qout / nr;
            }
            if (ring.size() == 2 * kAvg && step >= cfg.t_relax) {
                double m0 = 0.0, m1 = 0.0;
                for (size_t r_ = 0; r_ < kAvg; ++r_) {
                    m0 += ring[r_].dp / kAvg;
                    m1 += ring[kAvg + r_].dp / kAvg;
                }
                const double imb = out.q_in > 0.0 ? std::abs(out.q_in - out.q_out) / out.q_in : 1.0;
                stop = std::abs(m1) > 0.0 && std::abs(m1 - m0) / std::abs(m1) <
                                                 std::max(cfg.tol, kTurbulentTol) &&
                       imb < 0.01;
            }
        } else {
            out.q_in = qin_mean;
            out.q_out = qout_mean;
            out.rho_mean.swap(win_rho);
            if (step >= cfg.ramp_steps && std::isfinite(prev_dp) && std::abs(dp) > 0.0) {
                // An exponential approach changes by ~residual * dt / T per window, so scale
                // the window change by T / dt to estimate the remaining relative residual.
                const double change = std::abs(dp - prev_dp) / std::abs(dp);
                const double residual = change * std::max(1.0, cfg.t_relax / cfg.check_every);
                passes = (residual < cfg.tol && imbalance < 0.01) ? passes + 1 : 0;
                stop = passes >= 3;
            }
        }
        prev_dp = dp;
        if (stop) {
            out.converged = true;
            break;
        }
    }
    if (n_wall) {
        double s = 0.0, su = 0.0, mn = std::numeric_limits<double>::infinity(), mx = 0.0;
        for (size_t w = 0; w < n_wall; ++w) {
            const double yp = L.wall_y[w] * utau[w] / nu0;
            s += yp;
            su += utau[w];
            mn = std::min(mn, yp);
            mx = std::max(mx, yp);
        }
        out.yplus_mean = s / (double)n_wall;
        out.yplus_min = mn;
        out.yplus_max = mx;
        out.utau_mean = su / (double)n_wall;
        out.utau_wall = utau;
    }
    return out;
}

struct LineFit {
    bool ok = false;
    double a = 0.0, b = 0.0;  // p = a + b s
};
LineFit fit_line(const std::vector<double>& s, const std::vector<double>& p,
                 const std::vector<double>& w) {
    LineFit f;
    if (s.size() < 2) return f;
    double sw = 0, ss = 0, sp = 0, sss = 0, ssp = 0;
    for (size_t i = 0; i < s.size(); ++i) {
        sw += w[i];
        ss += w[i] * s[i];
        sp += w[i] * p[i];
        sss += w[i] * s[i] * s[i];
        ssp += w[i] * s[i] * p[i];
    }
    const double den = sw * sss - ss * ss;
    if (!(std::abs(den) > 1e-12 * std::max(1.0, sw * sss))) return f;
    f.b = (sw * ssp - ss * sp) / den;
    f.a = (sp - f.b * ss) / sw;
    f.ok = std::isfinite(f.a) && std::isfinite(f.b);
    return f;
}

int solver_threads(size_t nodes) {
    if (nodes < 8 * 1024 || chudware::pool::shared().in_worker()) return 1;
    const uint64_t est = (uint64_t)((double)nodes * kBytesPerFluidCell);
    const auto plan = chudware::governor::plan_for(est, "pipe_flow", 0);
    int n = std::max(1, std::min(plan.workers, 8));
    if (const char* e = std::getenv("CHUDWARE_LBM_THREADS")) {
        const int requested = std::atoi(e);
        if (requested >= 1) n = std::min(n, requested);
    }
#ifndef CHUDWARE_HAVE_OPENMP
    n = std::min(n, chudware::pool::shared().max_workers());
#endif
    return n;
}

void write_progress_file(const std::string& path, int step, int total) {
    if (path.empty()) return;
    char buf[160];
    std::snprintf(buf, sizeof buf,
                  "{\"pct\":%d,\"step\":%d,\"n_steps\":%d,\"stage\":\"pipe_flow_solve\"}",
                  (int)std::lround(100.0 * step / std::max(1, total)), step, total);
    // Overwrite in place (same convention as flow_run.cpp's progress side-channel).
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    if (f) f << buf;
}

}  // namespace

json run_pipe_flow(const std::vector<PipeFlowShell>& world_shells, const json& params,
                   PipeRunLossFn one_d_fn) {
    try {
        // ── Parameters ───────────────────────────────────────────────────────────────────
        Plane inlet, outlet;
        if (!parse_plane(params, "inlet", inlet))
            return refuse("invalid_params",
                          "inlet must be {point:[x,y,z], normal:[dx,dy,dz]} (mm) on the inlet face");
        if (!parse_plane(params, "outlet", outlet))
            return refuse("invalid_params",
                          "outlet must be {point:[x,y,z], normal:[dx,dy,dz]} (mm) on the outlet face");
        const double rho_phys = params.value("density_kgm3", 0.0);
        const double mu_phys = params.value("viscosity_pas", 0.0);
        if (!std::isfinite(rho_phys) || rho_phys <= 0.0 || !std::isfinite(mu_phys) ||
            mu_phys <= 0.0)
            return refuse("invalid_params",
                          "fluid density_kgm3 and viscosity_pas must be positive (or pass a "
                          "known fluid name)");
        const std::string fluid_name = params.value("fluid", std::string("custom"));
        const bool has_u =
            params.contains("mean_velocity_ms") && !params["mean_velocity_ms"].is_null();
        const bool has_q = params.contains("flow_m3s") && !params["flow_m3s"].is_null();
        if (has_u == has_q)
            return refuse("invalid_params", "give exactly one of mean_velocity_ms or flow_m3s");
        const double u_given = has_u ? params["mean_velocity_ms"].get<double>() : 0.0;
        const double q_given = has_q ? params["flow_m3s"].get<double>() : 0.0;
        if ((has_u && !(u_given > 0.0 && std::isfinite(u_given))) ||
            (has_q && !(q_given > 0.0 && std::isfinite(q_given))))
            return refuse("invalid_params", "the inlet velocity / flow rate must be positive");
        const bool cells_given = params.contains("cells_across");
        const int cells_across = params.value("cells_across", kDefaultCellsAcross);
        if (cells_across < 4 || cells_across > 128)
            return refuse("invalid_params", "cells_across must be in [4, 128]");
        const double voxel_req = params.value("voxel_mm", 0.0);
        if (!std::isfinite(voxel_req) || voxel_req < 0.0)
            return refuse("invalid_params", "voxel_mm must be positive when given");
        const double max_fluid = params.value("max_fluid_cells", kDefaultMaxFluidCells);
        if (!(max_fluid >= 1000.0)) return refuse("invalid_params", "max_fluid_cells must be >= 1000");
        const int max_steps_req = params.value("max_steps", 0);
        if (max_steps_req < 0) return refuse("invalid_params", "max_steps must be positive");
        const double roughness_mm = params.value("roughness_mm", 0.0);
        if (!std::isfinite(roughness_mm) || roughness_mm < 0.0)
            return refuse("invalid_params", "roughness_mm must be >= 0");
        const double tol = params.value("tolerance", 1e-3);
        if (!(tol > 0.0 && tol < 0.5))
            return refuse("invalid_params", "tolerance must be in (0, 0.5)");
        const std::string profile_req = params.value("inlet_profile", std::string("auto"));
        if (profile_req != "auto" && profile_req != "developed" && profile_req != "uniform")
            return refuse("invalid_params", "inlet_profile must be auto|developed|uniform");
        const std::string job_id = params.value("job_id", std::string());
        const std::string progress_path = params.value("progress_path", std::string());
        // Optional 1-D comparison: the aicad.piping/1 document + run this bore came from.
        const bool has_piping = params.contains("piping") && !params["piping"].is_null();
        const std::string run_id =
            params.contains("run_id") && params["run_id"].is_string()
                ? params["run_id"].get<std::string>()
                : std::string();
        if (has_piping != !run_id.empty())
            return refuse("invalid_params", "piping and run_id go together (the run of the "
                                            "aicad.piping/1 document this fluid volume bores)");
        int piping_bends = 0;
        double run_d_mm = 0.0, run_in_straight_mm = 0.0, run_out_straight_mm = 0.0;
        if (has_piping) {
            if (!params["piping"].is_object())
                return refuse("invalid_params", "piping must be an aicad.piping/1 object");
            if (!one_d_fn)
                return refuse("one_d_unavailable",
                              "this build has no piping 1-D loss model; omit piping/run_id");
            // Validate the run up front (and count its bends); the estimate itself is
            // evaluated after the solve at the flow the lattice carried.
            const json probe = one_d_fn(params["piping"], run_id, rho_phys, mu_phys,
                                        has_q ? q_given : u_given * 1e-3, roughness_mm);
            if (!probe.value("ok", false))
                return refuse(probe.value("reason", std::string("invalid_piping")),
                              probe.value("hint", std::string()));
            piping_bends = probe.value("bends", 0);
            for (const auto& ft : probe["fittings"])
                if (ft.value("type", std::string()) == "elbow") piping_bends = std::max(piping_bends, 1);
            run_d_mm = probe.value("inner_dia_mm", 0.0);
            run_in_straight_mm = probe.value("inlet_straight_mm", 0.0);
            run_out_straight_mm = probe.value("outlet_straight_mm", 0.0);
        }

        // ── Lattice frame: inlet axis -> +X, outlet axis in the XY plane ─────────────────
        // Straight runs, elbows and tees then present axis-aligned (and, at the inlet,
        // face-aligned) boundary planes to the lattice whatever their world orientation.
        const V3 e1 = inlet.n;
        V3 t_out = outlet.n - e1 * dot(outlet.n, e1);
        if (norm(t_out) < 1e-6)
            t_out = std::abs(e1.x) < 0.9 ? V3{1.0, 0.0, 0.0} - e1 * e1.x
                                         : V3{0.0, 1.0, 0.0} - e1 * e1.y;
        const V3 e2 = t_out * (1.0 / norm(t_out));
        const V3 e3 = cross(e1, e2);
        auto to_local = [&](const V3& p) { return V3{dot(p, e1), dot(p, e2), dot(p, e3)}; };
        auto to_world = [&](const V3& p) { return e1 * p.x + e2 * p.y + e3 * p.z; };
        inlet.p = to_local(inlet.p);
        inlet.n = {1.0, 0.0, 0.0};
        outlet.p = to_local(outlet.p);
        outlet.n = to_local(outlet.n);
        outlet.n.z = 0.0;
        if (std::abs(outlet.n.x) < 1e-12) outlet.n.x = 0.0;
        if (std::abs(outlet.n.y) < 1e-12) outlet.n.y = 0.0;
        outlet.n = outlet.n * (1.0 / norm(outlet.n));
        std::vector<PipeFlowShell> shells(world_shells.size());
        for (size_t b = 0; b < world_shells.size(); ++b) {
            const auto& wp = world_shells[b].pos;
            auto& lp = shells[b].pos;
            lp.resize(wp.size());
            for (size_t i = 0; i + 2 < wp.size(); i += 3) {
                const V3 q = to_local(V3{wp[i], wp[i + 1], wp[i + 2]});
                lp[i] = (float)q.x;
                lp[i + 1] = (float)q.y;
                lp[i + 2] = (float)q.z;
            }
            shells[b].idx = world_shells[b].idx;
        }

        // ── Geometry: closed shells, volume, area, bbox ─────────────────────────────────
        if (shells.empty())
            return refuse("no_geometry", "pass stl_path or a doc_id with a solid fluid volume");
        std::vector<std::vector<uint32_t>> idx(shells.size());
        double vol = 0.0, area = 0.0;
        // Area moments of the wall about the inlet axis: a straight duct (reducers included) is
        // a surface of revolution about it — lateral centroid on the axis, isotropic spread.
        double m_y = 0.0, m_z = 0.0, m_yy = 0.0, m_zz = 0.0, m_yz = 0.0;
        V3 lo, hi;
        auto measure = [&]() -> json {
            vol = area = m_y = m_z = m_yy = m_zz = m_yz = 0.0;
            lo = {1e300, 1e300, 1e300};
            hi = {-1e300, -1e300, -1e300};
            for (size_t b = 0; b < shells.size(); ++b) {
                const auto& s = shells[b];
                if (s.pos.size() < 9 || s.pos.size() % 3 || s.idx.size() < 3 || s.idx.size() % 3)
                    return refuse("invalid_geometry", "malformed triangle shell");
                const size_t nv = s.pos.size() / 3;
                for (uint32_t i : s.idx)
                    if ((size_t)i >= nv)
                        return refuse("invalid_geometry", "triangle index out of range");
                for (float val : s.pos)
                    if (!std::isfinite(val)) return refuse("invalid_geometry", "non-finite vertex");
                const size_t open = clean_shell(s, idx[b]);
                if (open)
                    return refuse("mesh_not_watertight",
                                  std::to_string(open) +
                                      " edge(s) are not shared by exactly two facets: the fluid "
                                      "volume must be one closed solid (e.g. "
                                      "piping_internal_volume or a capped bore); close the open "
                                      "ends and retry");
                double sv = 0.0;
                for (size_t t = 0; t < idx[b].size(); t += 3) {
                    V3 P[3];
                    for (int k = 0; k < 3; ++k) {
                        const size_t o = (size_t)idx[b][t + k] * 3;
                        P[k] = {s.pos[o], s.pos[o + 1], s.pos[o + 2]};
                        lo = {std::min(lo.x, P[k].x), std::min(lo.y, P[k].y),
                              std::min(lo.z, P[k].z)};
                        hi = {std::max(hi.x, P[k].x), std::max(hi.y, P[k].y),
                              std::max(hi.z, P[k].z)};
                    }
                    sv += dot(P[0], cross(P[1], P[2])) / 6.0;
                    const double ta = 0.5 * norm(cross(P[1] - P[0], P[2] - P[0]));
                    area += ta;
                    const double cy = (P[0].y + P[1].y + P[2].y) / 3.0 - inlet.p.y;
                    const double cz = (P[0].z + P[1].z + P[2].z) / 3.0 - inlet.p.z;
                    m_y += ta * cy;
                    m_z += ta * cz;
                    m_yy += ta * cy * cy;
                    m_zz += ta * cz * cz;
                    m_yz += ta * cy * cz;
                }
                vol += std::abs(sv);
            }
            if (!(vol > 0.0) || !(area > 0.0))
                return refuse("invalid_geometry", "the fluid volume encloses no volume");
            return nullptr;
        };
        if (json bad = measure(); !bad.is_null()) return bad;

        std::vector<std::string> warnings, caveats;
        const uint64_t mem_budget = chudware::governor::budget_bytes();
        const double mem_cells =
            mem_budget == UINT64_MAX ? max_fluid : (double)mem_budget / kBytesPerFluidCell;
        const double fluid_cap = std::max(1000.0, std::min(max_fluid, mem_cells));

        // ── Piping run: straight end legs of at least 5 D ─────────────────────────────────
        // Standard K-factors include the fitting's downstream recovery and assume developed
        // inflow, and a constant-pressure outlet on a bend's skewed exit flow adds a spurious
        // loss. So the run's end legs are lengthened to kEndLegDiameters straight diameters
        // (as far as the cell budget allows) and dp is read between the run's own end planes.
        double ext_in = 0.0, ext_out = 0.0;
        if (has_piping && run_d_mm > 0.0) {
            const double want_in = std::max(0.0, kEndLegDiameters * run_d_mm - run_in_straight_mm);
            const double want_out =
                std::max(0.0, kEndLegDiameters * run_d_mm - run_out_straight_mm);
            if (want_in + want_out > 0.0) {
                // Cell budget at the resolution the run will get (same defaults as below).
                const double u_est =
                    has_u ? u_given
                          : q_given / (0.25 * 3.14159265358979323846 * run_d_mm * run_d_mm * 1e-6);
                const double re_est = u_est * run_d_mm * 1e-3 / (mu_phys / rho_phys);
                int across = cells_across;
                if (!cells_given)
                    across = re_est >= 2300.0
                                 ? kBendCellsAcross
                                 : std::max(across, (int)std::min(96.0, std::ceil(re_est / 30.0)));
                const double v_pred = voxel_req > 0.0 ? voxel_req : run_d_mm / across;
                const double a_sec = 0.25 * 3.14159265358979323846 * run_d_mm * run_d_mm;
                const double room = fluid_cap * v_pred * v_pred * v_pred - vol;
                double scale = 1.0;
                if (a_sec * (want_in + want_out) > room)
                    scale = std::max(0.0, room / (a_sec * (want_in + want_out)));
                const double tol = std::max(1e-3, 2e-3 * run_d_mm);
                if (want_in * scale > 0.0 &&
                    extrude_cap(shells, inlet.p, inlet.n, want_in * scale, tol))
                    ext_in = want_in * scale;
                if (want_out * scale > 0.0 &&
                    extrude_cap(shells, outlet.p, outlet.n, want_out * scale, tol))
                    ext_out = want_out * scale;
                if ((want_in > 0.0 && ext_in < want_in) || (want_out > 0.0 && ext_out < want_out)) {
                    warnings.push_back("end_legs_short");
                    caveats.push_back(
                        "The run's straight end legs could not all be extended to " +
                        fmt("%.0f", kEndLegDiameters) + " D (inlet +" + fmt("%.0f", ext_in) +
                        " of " + fmt("%.0f", want_in) + " mm, outlet +" + fmt("%.0f", ext_out) +
                        " of " + fmt("%.0f", want_out) + " mm: " +
                        (scale < 1.0 ? "cell budget" : "no planar end cap on the port plane") +
                        "); losses next to a short leg read high.");
                }
                if (ext_in > 0.0 || ext_out > 0.0)
                    if (json bad = measure(); !bad.is_null()) return bad;
            }
        }
        const double d_est = 4.0 * vol / area;  // hydraulic-diameter estimate (exact for long ducts)
        // Does the inlet->outlet path change direction? (outlet axis off the inlet axis, outlet
        // displaced off it, or a wall that is not a surface of revolution about it). A piping
        // run's own corners/elbows count too.
        bool bent = piping_bends > 0;
        {
            const double cy = m_y / area, cz = m_z / area;
            const double syy = m_yy / area - cy * cy, szz = m_zz / area - cz * cz,
                         syz = m_yz / area - cy * cz;
            const double anis =
                std::sqrt((syy - szz) * (syy - szz) + 4.0 * syz * syz) / std::max(syy + szz, 1e-30);
            const V3 off = outlet.p - inlet.p;
            if (std::abs(outlet.n.x) < std::cos(5.0 * 3.14159265358979323846 / 180.0) ||
                std::hypot(off.y, off.z) > 0.25 * d_est || std::hypot(cy, cz) > 0.25 * d_est ||
                anis > 0.3)
                bent = true;
        }

        // ── Voxel size + budgets ─────────────────────────────────────────────────────────
        const V3 ext = hi - lo;
        auto dense_cells = [&](double vx) {
            return (ext.x / vx + 6.0) * (ext.y / vx + 6.0) * (ext.z / vx + 6.0);
        };
        double v = voxel_req;
        bool bend_default = false;  // the 16-cell bend default chose the resolution
        if (v > 0.0) {
            if (vol / (v * v * v) > fluid_cap || dense_cells(v) > kMaxDenseCells) {
                double v_min = std::cbrt(vol / fluid_cap);
                while (dense_cells(v_min) > kMaxDenseCells) v_min *= 1.05;
                return refuse("grid_too_large",
                              "voxel_mm " + fmt("%.3g", v) + " needs ~" +
                                  fmt("%.3g", vol / (v * v * v)) +
                                  " fluid cells; raise voxel_mm to at least " +
                                  fmt("%.3g", v_min) + " mm or simulate a shorter section");
            }
        } else {
            int across = cells_across;
            if (!cells_given) {
                // Turbulent default: the wall function carries the near-wall layer, and it is
                // validated from 10 to 20 cells across D_h, so 12 keeps straight runs fast;
                // a path that bends gets 16 (bend loss over-prediction 1.47-1.85x Rennels vs
                // 2.0-2.3x at 12). Laminar: keep the cell Reynolds number Re/N under ~30 so
                // the near-1/2 relaxation the physical Re needs stays stable.
                const double nu = mu_phys / rho_phys;
                const double u_est =
                    has_u ? u_given
                          : q_given / (0.25 * 3.14159265358979323846 * d_est * d_est * 1e-6);
                const double re_est = u_est * d_est * 1e-3 / nu;
                if (re_est >= 2300.0) {
                    across = bent ? kBendCellsAcross : kTurbulentCellsAcross;
                    bend_default = bent;
                } else {
                    across = std::max(across, (int)std::min(96.0, std::ceil(re_est / 30.0)));
                }
            }
            v = d_est / across;
            double v_final = std::max(v, std::cbrt(vol / fluid_cap));
            while (dense_cells(v_final) > kMaxDenseCells) v_final *= 1.05;
            if (v_final > v * 1.0001) {
                warnings.push_back("grid_coarsened");
                if (bend_default) warnings.push_back("bend_resolution_reduced");
                caveats.push_back("Voxel coarsened from " + fmt("%.3g", v) + " to " +
                                  fmt("%.3g", v_final) +
                                  " mm to fit the cell/memory budget (max_fluid_cells, "
                                  "CHUDWARE_MEM_FRAC); simulate a shorter section for more "
                                  "resolution.");
            }
            v = v_final;
        }
        if (d_est / v < 3.0)
            return refuse("grid_too_coarse",
                          "the flow passage (~" + fmt("%.3g", d_est) +
                              " mm) spans fewer than 3 voxels at " + fmt("%.3g", v) +
                              " mm; lower voxel_mm or simulate a shorter section of the network");
        {
            auto lattice_bytes = [&](double vx) {
                return (uint64_t)(vol / (vx * vx * vx) * kBytesPerFluidCell +
                                  dense_cells(vx) * kBytesPerDenseCell);
            };
            std::string why;
            if (!chudware::governor::admit(lattice_bytes(v), "pipe-flow lattice", why)) {
                // The 16-cell bend default does not fit: fall back to the straight-pipe
                // default before refusing, and say so.
                const double v12 = d_est / kTurbulentCellsAcross;
                std::string why12;
                if (!bend_default || !(v12 > v) ||
                    !chudware::governor::admit(lattice_bytes(v12), "pipe-flow lattice", why12))
                    return refuse("insufficient_memory", why);
                warnings.push_back("bend_resolution_reduced");
                caveats.push_back("The " + std::to_string(kBendCellsAcross) +
                                  "-cells-across bend default (" + fmt("%.3g", v) +
                                  " mm voxels) did not fit the memory budget (" + why +
                                  "); ran at " + std::to_string(kTurbulentCellsAcross) +
                                  " cells across (" + fmt("%.3g", v12) +
                                  " mm), where bend losses read higher.");
                v = v12;
            }
        }

        // ── Grid (face-aligned to the inlet plane along its dominant axis) + voxelize ─────
        Grid g;
        g.v = v;
        g.o = {lo.x - 2.0 * v, lo.y - 2.0 * v, lo.z - 2.0 * v};
        int in_axis = 0;
        {
            const double ax = std::abs(inlet.n.x), ay = std::abs(inlet.n.y),
                         az = std::abs(inlet.n.z);
            in_axis = (ax >= ay && ax >= az) ? 0 : (ay >= az ? 1 : 2);
            const double pk = comp(inlet.p, in_axis), lk = comp(lo, in_axis);
            const double ok = pk - v * std::ceil((pk - lk) / v + 2.0);
            if (in_axis == 0) g.o.x = ok;
            else if (in_axis == 1) g.o.y = ok;
            else g.o.z = ok;
        }
        g.nx = (int)std::ceil((hi.x + 2.0 * v - g.o.x) / v) + 1;
        g.ny = (int)std::ceil((hi.y + 2.0 * v - g.o.y) / v) + 1;
        g.nz = (int)std::ceil((hi.z + 2.0 * v - g.o.z) / v) + 1;
        if ((double)g.nx * g.ny * g.nz > kMaxDenseCells * 1.2)
            return refuse("grid_too_large",
                          "the voxel grid bounding the fluid volume is too large; raise "
                          "voxel_mm or simulate a shorter section");
        report_progress("pipe_flow_voxelize");
        std::vector<uint8_t> state(g.size(), kSolid);
        for (size_t b = 0; b < shells.size(); ++b) {
            std::vector<float> shifted = shells[b].pos;
            for (size_t i = 0; i + 2 < shifted.size(); i += 3) {
                shifted[i] = (float)(shifted[i] - g.o.x);
                shifted[i + 1] = (float)(shifted[i + 1] - g.o.y);
                shifted[i + 2] = (float)(shifted[i + 2] - g.o.z);
            }
            voxelize_body(shifted, idx[b], state, g.nx, g.ny, g.nz, v, /*center_sample=*/true);
        }
        std::vector<size_t> fluid;
        for (size_t c = 0; c < state.size(); ++c)
            if (state[c] == kFluid) fluid.push_back(c);
        if (fluid.empty())
            return refuse("no_fluid_cells", "no voxel center falls inside the fluid volume at " +
                                                fmt("%.3g", v) + " mm; lower voxel_mm");
        report_progress("pipe_flow_voxelize", fluid.size());

        // ── Inlet / outlet layers ────────────────────────────────────────────────────────
        const double seed_limit = std::max(3.0 * v, d_est);
        auto no_cells = [&](const Plane& pl, const std::string& which) {
            return refuse("no_" + which + "_cells",
                          "the " + which + " plane at " + vec_str(to_world(pl.p)) +
                              " (normal " + vec_str(to_world(pl.n)) +
                              ") does not cut the fluid volume within " +
                              fmt("%.3g", seed_limit) + " mm; put the point on the " + which +
                              " end face and the normal along the pipe axis");
        };
        std::vector<size_t> in_cells, out_cells;
        if (orient_plane(g, fluid, inlet) <= seed_limit)
            in_cells = plane_layer(g, state, fluid, inlet, seed_limit);
        if (in_cells.empty()) return no_cells(inlet, "inlet");
        for (size_t c : in_cells) state[c] = kInlet;
        if (orient_plane(g, fluid, outlet) <= seed_limit)
            out_cells = plane_layer(g, state, fluid, outlet, seed_limit);
        if (out_cells.empty()) {
            // The outlet layer can come up empty because the inlet layer already took it.
            for (size_t c : in_cells) state[c] = kFluid;
            const bool overlap = !plane_layer(g, state, fluid, outlet, seed_limit).empty();
            if (overlap)
                return refuse("inlet_outlet_overlap",
                              "the inlet and outlet planes select the same voxels; move them "
                              "apart along the flow path");
            return no_cells(outlet, "outlet");
        }
        for (size_t c : out_cells) state[c] = kOutlet;

        // Interior: everything 18-connected to the downstream side of the inlet layer.
        std::vector<size_t> stack;
        for (size_t c : in_cells)
            for (int q = 1; q < NQ; ++q) {
                if (CX[q] * inlet.n.x + CY[q] * inlet.n.y + CZ[q] * inlet.n.z <= 0.0) continue;
                const size_t nb = g.step(c, CX[q], CY[q], CZ[q]);
                if (nb != SIZE_MAX && state[nb] == kFluid) {
                    state[nb] = kInterior;
                    stack.push_back(nb);
                }
            }
        while (!stack.empty()) {
            const size_t c = stack.back();
            stack.pop_back();
            for (int q = 1; q < NQ; ++q) {
                const size_t nb = g.step(c, CX[q], CY[q], CZ[q]);
                if (nb != SIZE_MAX && state[nb] == kFluid) {
                    state[nb] = kInterior;
                    stack.push_back(nb);
                }
            }
        }
        bool outlet_reached = false;
        for (size_t c : out_cells) {
            for (int q = 1; q < NQ && !outlet_reached; ++q) {
                const size_t nb = g.step(c, CX[q], CY[q], CZ[q]);
                outlet_reached = nb != SIZE_MAX && state[nb] == kInterior;
            }
            if (outlet_reached) break;
        }
        if (!outlet_reached)
            return refuse("inlet_outlet_disconnected",
                          "no fluid path connects the inlet layer to the outlet layer at " +
                              fmt("%.3g", v) +
                              " mm voxels; check both planes sit on the same passage, or lower "
                              "voxel_mm if a narrow section closes up");

        // ── Nodes + streaming tables ─────────────────────────────────────────────────────
        Lattice L;
        std::vector<int32_t> node_of(g.size(), -1);
        for (size_t c = 0; c < state.size(); ++c)
            if (state[c] == kInterior) {
                node_of[c] = (int32_t)L.cell.size();
                L.cell.push_back(c);
            }
        L.n_int = L.cell.size();
        for (size_t c : in_cells) {
            node_of[c] = (int32_t)L.cell.size();
            L.cell.push_back(c);
        }
        L.n_in = in_cells.size();
        for (size_t c : out_cells) {
            node_of[c] = (int32_t)L.cell.size();
            L.cell.push_back(c);
        }
        L.n_out = out_cells.size();
        if (L.n_int < 8)
            return refuse("pipe_too_short",
                          "fewer than 8 interior voxels between the inlet and outlet planes; "
                          "lower voxel_mm or move the planes apart");
        L.nbr.assign(L.n_int * NQ, -1);
        for (size_t i = 0; i < L.n_int; ++i)
            for (int q = 0; q < NQ; ++q) {
                const size_t src = g.step(L.cell[i], -CX[q], -CY[q], -CZ[q]);
                L.nbr[i * NQ + q] = src == SIZE_MAX ? -1 : node_of[src];
            }
        const size_t n_bc = L.n_in + L.n_out;
        L.bc_nb.assign(n_bc, -1);
        for (size_t k = 0; k < n_bc; ++k) {
            const size_t b = L.n_int + k;
            const V3& n = k < L.n_in ? inlet.n : outlet.n;
            auto& links = k < L.n_in ? L.in_links : L.out_links;
            double best = 0.0;
            for (int q = 1; q < NQ; ++q) {
                const size_t t = g.step(L.cell[b], CX[q], CY[q], CZ[q]);
                if (t == SIZE_MAX || state[t] != kInterior) continue;
                links.push_back({(int32_t)b, q, node_of[t]});
                const double cl =
                    std::sqrt((double)(CX[q] * CX[q] + CY[q] * CY[q] + CZ[q] * CZ[q]));
                const double align = (CX[q] * n.x + CY[q] * n.y + CZ[q] * n.z) / cl;
                if (align > best) {
                    best = align;
                    L.bc_nb[k] = node_of[t];
                }
            }
        }

        // Flow-path distance (mm) from the inlet plane: Dijkstra with a 5x5x5 chamfer (every
        // primitive move with components in [-2,2], length-2 moves only through fluid), which
        // keeps the metric within ~2% of Euclidean in any direction.
        const size_t N = L.nodes();
        std::vector<double> dist(N, std::numeric_limits<double>::infinity());
        const std::vector<Move> moves = chamfer_moves();
        auto is_node = [&](size_t c) { return c != SIZE_MAX && node_of[c] >= 0; };
        auto passable = [&](size_t cu, const Move& mv) {
            return !mv.via || (is_node(g.step(cu, mv.lo[0], mv.lo[1], mv.lo[2])) &&
                               is_node(g.step(cu, mv.hi[0], mv.hi[1], mv.hi[2])));
        };
        using Item = std::pair<double, int32_t>;
        {
            std::priority_queue<Item, std::vector<Item>, std::greater<Item>> pq;
            for (size_t k = 0; k < L.n_in; ++k) {
                const size_t b = L.n_int + k;
                dist[b] = std::max(0.0, dot(g.center(L.cell[b]) - inlet.p, inlet.n));
                pq.push({dist[b], (int32_t)b});
            }
            while (!pq.empty()) {
                const auto [d, u] = pq.top();
                pq.pop();
                if (d > dist[(size_t)u]) continue;
                if ((size_t)u >= L.n_int + L.n_in) continue;  // outlet nodes end paths
                const size_t cu = L.cell[(size_t)u];
                for (const Move& mv : moves) {
                    const size_t t = g.step(cu, mv.d[0], mv.d[1], mv.d[2]);
                    if (!is_node(t)) continue;
                    const int32_t w = node_of[t];
                    // Paths never re-enter the inlet layer (it is the source).
                    if ((size_t)w >= L.n_int && (size_t)w < L.n_int + L.n_in) continue;
                    if (!passable(cu, mv)) continue;
                    const double nd = d + mv.len * v;
                    if (nd < dist[(size_t)w]) {
                        dist[(size_t)w] = nd;
                        pq.push({nd, w});
                    }
                }
            }
        }

        // Walls: interior nodes with bounce-back links. The wall normal is the unit sum of
        // w_q c_q over those links (so a tangential wall velocity injects no mass), the wall
        // distance the nearest half-way link point; a chamfer Dijkstra spreads distance and
        // the nearest wall node to every interior node (turbulence model + inlet profile).
        L.wall_of.assign(L.n_int, -1);
        for (size_t i = 0; i < L.n_int; ++i) {
            V3 m, faces;
            double yw = std::numeric_limits<double>::infinity();
            for (int q = 1; q < NQ; ++q) {
                if (L.nbr[i * NQ + q] >= 0) continue;
                const V3 c{(double)CX[q], (double)CY[q], (double)CZ[q]};
                m = m + c * W[q];
                if (q <= 6) faces = faces + c;
                yw = std::min(yw, 0.5 * norm(c));
            }
            if (!std::isfinite(yw)) continue;
            L.wall_of[i] = (int32_t)L.wall_node.size();
            L.wall_node.push_back((int32_t)i);
            const double mn = norm(m);
            L.wall_n.push_back(mn > 1e-12 ? m * (1.0 / mn) : V3{});
            L.wall_y.push_back(yw);
            // Projected wall area: the vector sum of the cell's solid faces (a flat face is 1,
            // a 45-degree stair step sqrt 2). Cells touching the wall only along an edge carry
            // none; their neighbours' faces already account for that stretch of wall.
            L.wall_area.push_back(norm(faces));
        }
        L.y.assign(L.n_int, std::numeric_limits<double>::infinity());
        L.near_wall.assign(L.n_int, -1);
        {
            std::priority_queue<Item, std::vector<Item>, std::greater<Item>> pq;
            for (size_t w = 0; w < L.wall_node.size(); ++w) {
                const size_t i = (size_t)L.wall_node[w];
                L.y[i] = L.wall_y[w];
                L.near_wall[i] = (int32_t)w;
                pq.push({L.y[i], (int32_t)i});
            }
            while (!pq.empty()) {
                const auto [d, u] = pq.top();
                pq.pop();
                if (d > L.y[(size_t)u]) continue;
                const size_t cu = L.cell[(size_t)u];
                for (const Move& mv : moves) {
                    const size_t t = g.step(cu, mv.d[0], mv.d[1], mv.d[2]);
                    if (!is_node(t)) continue;
                    const int32_t w = node_of[t];
                    if ((size_t)w >= L.n_int || !passable(cu, mv)) continue;
                    const double nd = d + mv.len;
                    if (nd < L.y[(size_t)w]) {
                        L.y[(size_t)w] = nd;
                        L.near_wall[(size_t)w] = L.near_wall[(size_t)u];
                        pq.push({nd, w});
                    }
                }
            }
            for (double& yy : L.y)
                if (!std::isfinite(yy)) yy = 0.5;
        }
        double out_node_dist = 0.0, out_plane_off = 0.0;
        size_t out_counted = 0;
        for (size_t k = 0; k < L.n_out; ++k) {
            const size_t b = L.n_int + L.n_in + k;
            if (!std::isfinite(dist[b])) continue;
            out_node_dist += dist[b];
            out_plane_off += std::max(0.0, dot(g.center(L.cell[b]) - outlet.p, outlet.n));
            ++out_counted;
        }
        if (!out_counted)
            return refuse("inlet_outlet_disconnected",
                          "the outlet layer is not reachable along the flow path");
        out_node_dist /= (double)out_counted;
        out_plane_off /= (double)out_counted;
        const double path_mm = out_node_dist + out_plane_off;

        // ── Inlet section: area + wetted perimeter from the exact mesh cut ────────────────
        V3 blo{1e300, 1e300, 1e300}, bhi{-1e300, -1e300, -1e300};
        for (size_t c : in_cells) {
            const V3 ctr = g.center(c);
            blo = {std::min(blo.x, ctr.x), std::min(blo.y, ctr.y), std::min(blo.z, ctr.z)};
            bhi = {std::max(bhi.x, ctr.x), std::max(bhi.y, ctr.y), std::max(bhi.z, ctr.z)};
        }
        blo = blo - V3{1.5 * v, 1.5 * v, 1.5 * v};
        bhi = bhi + V3{1.5 * v, 1.5 * v, 1.5 * v};
        Plane cut = inlet;
        cut.p = inlet.p + inlet.n * (0.5 * v);
        const Section sec = slice_section(shells, idx, cut, blo, bhi);
        double area_mm2 = sec.area_mm2, dh_mm = 0.0;
        if (sec.area_mm2 > 0.0 && sec.perimeter_mm > 0.0) {
            dh_mm = 4.0 * sec.area_mm2 / sec.perimeter_mm;
        } else {
            area_mm2 = (double)L.n_in * v * v;
            dh_mm = d_est;
            warnings.push_back("inlet_section_estimated");
            caveats.push_back("The inlet plane cut no clean mesh section; the inlet area is the "
                              "voxel count and the hydraulic diameter is 4V/S of the volume.");
        }
        const double area_m2 = area_mm2 * 1e-6;
        const double u_phys = has_u ? u_given : q_given / area_m2;
        const double nu_phys = mu_phys / rho_phys;
        const double re_phys = u_phys * (dh_mm * 1e-3) / nu_phys;
        const std::string regime =
            re_phys < 2300.0 ? "laminar" : (re_phys <= 4000.0 ? "transitional" : "turbulent");
        const double d_lat = dh_mm / v;
        const double l_lat = path_mm / v;
        const double a_lat = area_mm2 / (v * v);  // physical inlet area in lattice units

        const bool turbulent = re_phys >= 2300.0;

        // ── Inlet profile ────────────────────────────────────────────────────────────────
        // Laminar: the developed (Poisson) profile of the inlet section. Turbulent: the
        // power law u ~ y^(1/n), n from Re, over the wall distance of each inlet node.
        std::string profile_used = "uniform";
        std::vector<double> prof;
        const double n_pow =
            std::clamp(-1.7 + 1.8 * std::log10(std::max(re_phys, 4000.0)), 6.0, 10.0);
        double y_in_max = 0.0, power_mean = 1.0;
        auto inlet_y = [&](size_t k) {
            const int32_t nb = L.bc_nb[k];
            return nb >= 0 ? L.y[(size_t)nb] : 0.5;
        };
        for (size_t k = 0; k < L.n_in; ++k) y_in_max = std::max(y_in_max, inlet_y(k));
        if (profile_req != "uniform") {
            if (!turbulent) {
                std::vector<int32_t> local(g.size(), -1);
                for (size_t k = 0; k < in_cells.size(); ++k) local[in_cells[k]] = (int32_t)k;
                prof = developed_profile(g, state, in_cells, local, in_axis);
                if (!prof.empty()) profile_used = "developed_laminar";
                else warnings.push_back("inlet_profile_uniform_fallback");
            } else if (y_in_max > 0.0) {
                prof.resize(L.n_in);
                double mean = 0.0;
                for (size_t k = 0; k < L.n_in; ++k) {
                    prof[k] = std::pow(std::min(1.0, inlet_y(k) / y_in_max), 1.0 / n_pow);
                    mean += prof[k];
                }
                power_mean = mean / (double)L.n_in;
                for (double& p : prof) p /= power_mean;
                profile_used = "power_law_1/" + fmt("%.1f", n_pow);
            }
        }
        if (prof.empty()) prof.assign(L.n_in, 1.0);
        const double psi_max = *std::max_element(prof.begin(), prof.end());
        L.inlet_u.resize(L.n_in);
        for (size_t k = 0; k < L.n_in; ++k) L.inlet_u[k] = inlet.n * prof[k];
        // Node speed per unit mean speed so the injected flux U*A_lat matches the physical
        // inlet area rather than the staircase voxel count.
        const double area_ratio = a_lat / (double)L.n_in;
        L.inlet_n = inlet.n;
        std::string inflow = profile_used;
        int k_recycle = 0, k_leg = 0;  // recycle plane / straight inlet leg, cells along +X
        if (turbulent && profile_req != "uniform") {
            // Recycling plane: up to 4 hydraulic diameters (60% of the straight inlet leg —
            // the run of +X cells that keeps the whole inlet section fluid) downstream.
            int k_max = 0;
            for (int k = 1; k < g.nx; ++k) {
                size_t hit = 0;
                for (size_t c : in_cells) {
                    const size_t t = g.step(c, k, 0, 0);
                    if (t != SIZE_MAX && state[t] == kInterior) ++hit;
                }
                if ((double)hit < 0.98 * (double)in_cells.size()) break;
                k_max = k;
            }
            const int k_r = (int)std::min(0.6 * k_max, 4.0 * d_lat);
            if (k_r >= std::max(3.0, d_lat)) {
                k_recycle = k_r;
                k_leg = k_max;
                L.recycle.assign(L.n_in, -1);
                for (size_t k = 0; k < L.n_in; ++k) {
                    const size_t t = g.step(in_cells[k], k_r, 0, 0);
                    if (t != SIZE_MAX && state[t] == kInterior) L.recycle[k] = node_of[t];
                }
                inflow = "recycled_developed (" + fmt("%.1f", k_r / d_lat) + " D_h downstream)";
            }
        }
        // STL/doc callers cannot be extended: say so when the outlet sits next to a bend
        // (its straight leg — the run of cells behind the outlet section that stays fluid
        // along the outlet axis — is under 3 D_h).
        if (!has_piping && bent) {
            int sx = 0, sy = 0;
            if (std::abs(outlet.n.x) > 0.999) sx = outlet.n.x > 0.0 ? 1 : -1;
            else if (std::abs(outlet.n.y) > 0.999) sy = outlet.n.y > 0.0 ? 1 : -1;
            if (sx || sy) {
                int k_out = 0;
                for (int k = 1; k < std::max(g.nx, g.ny); ++k) {
                    size_t hit = 0;
                    for (size_t c : out_cells) {
                        const size_t t = g.step(c, k * sx, k * sy, 0);
                        if (t != SIZE_MAX && state[t] == kInterior) ++hit;
                    }
                    if ((double)hit < 0.98 * (double)out_cells.size()) break;
                    k_out = k;
                }
                if (k_out < 3.0 * d_lat) {
                    warnings.push_back("outlet_at_bend_exit");
                    caveats.push_back(
                        "The outlet plane sits " + fmt("%.1f", k_out / d_lat) +
                        " D_h downstream of a bend: the constant-pressure outlet on the bend's "
                        "skewed exit flow adds a spurious loss (about one dynamic head on a "
                        "routed 4-elbow line) and the bend's downstream recovery is not "
                        "captured; extend the outlet leg to >= 5 D, or pass piping + run_id so "
                        "the end legs are extended automatically.");
                }
            }
        }

        // ── Lattice parameters (always at the physical Reynolds number) ──────────────────
        const double nu_pref = (kTauPreferred - 0.5) * CS2;
        const double u_cap =
            std::min(kUMax, kUNodeMax / (psi_max * std::max(area_ratio, 1e-6)));
        const double f_est = darcy_friction_factor(std::max(re_phys, 1e-9), 0.0);
        double u_lat =
            std::min(u_cap, std::sqrt(2.0 * kDrhoBudget / (f_est * l_lat / d_lat + 2.0)));
        double nu_lat = u_lat * d_lat / re_phys;
        if (!turbulent && nu_lat > nu_pref) {
            nu_lat = nu_pref;
            u_lat = re_phys * nu_lat / d_lat;
        }
        const double tau_mol = 3.0 * nu_lat + 0.5;
        SolveCfg cfg;
        cfg.collision = turbulent ? Collision::Rans
                                  : (tau_mol >= 0.55 ? Collision::Trt : Collision::Regularized);
        cfg.u_node = u_lat * area_ratio;
        cfg.nu0 = nu_lat;
        cfg.l_cap = kMixingCap * 0.5 * d_lat;
        cfg.r_core = 0.5 * d_lat;
        {
            const double r_lat = 0.5 * d_lat;
            double t_relax;
            if (turbulent) {
                // Core eddy viscosity ~0.07 u_tau R sets the momentum-diffusion time; the
                // initial field below starts close to developed, so a quarter of it suffices.
                const double nut_core = 0.07 * u_lat * std::sqrt(f_est / 8.0) * r_lat;
                t_relax = 0.25 * r_lat * r_lat / (nu_lat + nut_core) + 2.0 * l_lat / u_lat;
            } else {
                const double t_visc = r_lat * r_lat / nu_lat;
                const double t_adv = l_lat / u_lat * std::min(1.0, re_phys);
                const double t_press = 8.0 * nu_lat * l_lat * l_lat / (CS2 * r_lat * r_lat);
                t_relax = t_visc + t_adv + t_press;
            }
            t_relax = std::max(200.0, t_relax);
            cfg.t_relax = t_relax;
            cfg.check_every = (int)std::clamp(t_relax / 25.0, 25.0, 500.0);
            cfg.ramp_steps = turbulent ? 0 : (int)std::min(2000.0, 0.2 * t_relax);
            cfg.max_steps = max_steps_req > 0
                                ? max_steps_req
                                : (int)std::clamp(12.0 * t_relax, 2000.0, 200000.0);
        }
        cfg.tol = tol;

        // Turbulent start: the power-law profile along the flow-path direction (gradient of
        // the inlet distance field) with the Darcy pressure gradient, so the RANS run starts
        // near its developed state instead of diffusing momentum in from rest.
        std::vector<V3> u0;
        std::vector<double> rho0;
        if (turbulent) {
            u0.assign(N, V3{});
            rho0.assign(N, 1.0);
            const double grad = f_est / d_lat * 0.5 * u_lat * u_lat;  // lattice dp per cell
            auto node_dist = [&](size_t c) {
                if (c == SIZE_MAX || node_of[c] < 0) return std::numeric_limits<double>::quiet_NaN();
                return dist[(size_t)node_of[c]];
            };
            for (size_t i = 0; i < L.n_int; ++i) {
                const size_t c = L.cell[i];
                double gr[3];
                for (int k = 0; k < 3; ++k) {
                    const int dx = k == 0, dy = k == 1, dz = k == 2;
                    const double dp_ = node_dist(g.step(c, dx, dy, dz));
                    const double dm_ = node_dist(g.step(c, -dx, -dy, -dz));
                    const bool okp = std::isfinite(dp_), okm = std::isfinite(dm_);
                    gr[k] = okp && okm ? 0.5 * (dp_ - dm_)
                                       : (okp ? dp_ - dist[i] : (okm ? dist[i] - dm_ : 0.0));
                }
                V3 t{gr[0], gr[1], gr[2]};
                const double tn = norm(t);
                t = tn > 1e-12 ? t * (1.0 / tn) : inlet.n;
                const double psi =
                    y_in_max > 0.0
                        ? std::pow(std::min(1.0, L.y[i] / y_in_max), 1.0 / n_pow) / power_mean
                        : 1.0;
                u0[i] = t * (u_lat * psi);
                if (std::isfinite(dist[i]))
                    rho0[i] = 1.0 + 3.0 * grad * std::max(0.0, l_lat - dist[i] / v);
            }
            for (size_t k = 0; k < L.n_in; ++k) {
                u0[L.n_int + k] = L.inlet_u[k] * cfg.u_node;
                rho0[L.n_int + k] = 1.0 + 3.0 * grad * l_lat;
            }
            for (size_t k = 0; k < L.n_out; ++k) {
                const int32_t nb = L.bc_nb[L.n_in + k];
                if (nb >= 0) u0[L.n_int + L.n_in + k] = u0[(size_t)nb];
            }
            cfg.u_init = &u0;
            cfg.rho_init = &rho0;
        }

        const SimCancelToken cancel_tok = sim_job_register(job_id);
        struct JobGuard {
            const std::string& id;
            const SimCancelToken& token;
            ~JobGuard() { sim_job_unregister(id, token); }
        } job_guard{job_id, cancel_tok};
        std::function<bool()> should_cancel;
        if (cancel_tok) should_cancel = [cancel_tok] { return cancel_tok->load(); };

        const int threads = solver_threads(N);
        cfg.threads = threads;
        auto last_file = std::chrono::steady_clock::now();
        const double vs_est = u_phys / u_lat;
        auto on_check = [&](int step, double dp_lat, double imb, double drho) {
            if (progress_enabled()) {
                auto event = progress_detail::event("pipe_flow_solve", N);
                event["pct"] = 100.0 * step / cfg.max_steps;
                event["step"] = step;
                event["n_steps"] = cfg.max_steps;
                event["iteration"] = {
                    {"index", step}, {"max", cfg.max_steps},
                    {"objective", dp_lat * rho_phys * vs_est * vs_est},
                    {"metrics", {{"dp_pa", dp_lat * rho_phys * vs_est * vs_est},
                                 {"mass_imbalance", imb},
                                 {"max_density_deviation", drho}}}};
                progress_detail::emit(event);
            }
            const auto now = std::chrono::steady_clock::now();
            if (std::chrono::duration<double>(now - last_file).count() >= 1.0) {
                last_file = now;
                write_progress_file(progress_path, step, cfg.max_steps);
            }
        };
        const SolveOut S = solve_lattice(L, cfg, should_cancel, on_check);
        if (S.cancelled)
            return json{{"ok", false},
                        {"reason", "cancelled"},
                        {"hint", "sim_cancel stopped the run at step " + std::to_string(S.steps)},
                        {"steps", S.steps}};
        if (S.diverged)
            return refuse("solver_diverged",
                          "the lattice went unstable at step " + std::to_string(S.steps) +
                              " (molecular tau " + fmt("%.5f", tau_mol) +
                              "); lower voxel_mm (more cells across the passage) or the flow "
                              "rate, or use run_cfd_accurate (OpenFOAM) for this case");
        write_progress_file(progress_path, cfg.max_steps, cfg.max_steps);
        if (progress_enabled()) {
            auto event = progress_detail::event("pipe_flow_solve", N);
            event["pct"] = 100.0;
            event["step"] = S.steps;
            event["n_steps"] = cfg.max_steps;
            progress_detail::emit(event);
        }

        // ── Post-processing (lattice -> physical) ────────────────────────────────────────
        // Velocity scale from the measured lattice flow rate: Q_phys <-> Q_lat dx^3/dt.
        const double u_eff_lat = S.q_in / a_lat;
        if (!(u_eff_lat > 0.0) || !std::isfinite(u_eff_lat))
            return refuse("solver_diverged", "no net flow entered the domain; check that the "
                                             "inlet normal points along the passage");
        const double vs = u_phys / u_eff_lat;  // (m/s) per lattice speed
        const double p_scale = rho_phys * vs * vs;
        const double mass_imbalance = std::abs(S.q_in - S.q_out) / S.q_in;
        const double re_sim = u_eff_lat * d_lat / nu_lat;

        // Pressure profile: bin interior + outlet nodes by flow-path distance (the inlet
        // nodes carry their neighbour's density, so they are left out).
        const int nbins = std::max(1, (int)std::ceil(path_mm / v) + 1);
        std::vector<double> bs(nbins, 0.0), bp(nbins, 0.0), bw(nbins, 0.0);
        for (size_t i = 0; i < N; ++i) {
            if (i >= L.n_int && i < L.n_int + L.n_in) continue;
            if (!std::isfinite(dist[i])) continue;
            const int k = std::clamp((int)(dist[i] / v), 0, nbins - 1);
            bs[k] += dist[i];
            bp[k] += CS2 * (S.rho_mean[i] - 1.0) * p_scale;
            bw[k] += 1.0;
        }
        std::vector<double> ps, pp, pw;
        for (int k = 0; k < nbins; ++k)
            if (bw[k] > 0) {
                ps.push_back(bs[k] / bw[k]);
                pp.push_back(bp[k] / bw[k]);
                pw.push_back(bw[k]);
            }
        // Extrapolate each end to its plane with a line fit over the nearest few voxels.
        auto window_fit = [&](double s0, double s1) {
            std::vector<double> s, p, w;
            for (size_t k = 0; k < ps.size(); ++k)
                if (ps[k] >= s0 && ps[k] <= s1) {
                    s.push_back(ps[k]);
                    p.push_back(pp[k]);
                    w.push_back(pw[k]);
                }
            return fit_line(s, p, w);
        };
        const double s_first = ps.empty() ? 0.0 : ps.front();
        const LineFit fit_in = window_fit(s_first, s_first + 3.0 * v);
        const LineFit fit_out = window_fit(path_mm - 4.0 * v, path_mm);
        double p_in_plane =
            fit_in.ok ? fit_in.a : layer_pressure(S.rho_mean, L.n_int, L.n_in) * p_scale;
        // Recycled (fully developed) turbulent inflow: the inlet-plane pressure is the
        // recycle-plane pressure carried back along the developed gradient of the inlet leg,
        // which the pipe force balance gives from the wall-function stress around the recycle
        // plane, (C_w sum u_tau^2 A_wall) / (A_section * slab length). The inflow region's
        // adjustment from the imposed profile is then not counted as a pressure loss.
        double developed_grad_pa_mm = std::numeric_limits<double>::quiet_NaN();
        if (k_recycle > 0 && S.utau_wall.size() == L.wall_node.size()) {
            double s_r = 0.0;
            size_t n_r = 0;
            for (int32_t s : L.recycle)
                if (s >= 0 && std::isfinite(dist[(size_t)s])) {
                    s_r += dist[(size_t)s];
                    ++n_r;
                }
            if (n_r) {
                s_r /= (double)n_r;
                const double half = std::min(0.5 * dh_mm, std::max(v, k_leg * v - s_r));
                const double s_lo = s_r - half, s_hi = s_r + half;
                double force = 0.0;
                for (size_t w = 0; w < L.wall_node.size(); ++w) {
                    const double dw = dist[(size_t)L.wall_node[w]];
                    if (dw >= s_lo && dw < s_hi)
                        force += kWallStressCoef * S.utau_wall[w] * S.utau_wall[w] *
                                 L.wall_area[w];
                }
                const LineFit at_r = window_fit(s_r - 1.5 * v, s_r + 1.5 * v);
                if (force > 0.0 && at_r.ok) {
                    const double g_lat = force / (a_lat * (s_hi - s_lo) / v);
                    developed_grad_pa_mm = g_lat * p_scale / v;
                    p_in_plane = at_r.a + at_r.b * s_r + developed_grad_pa_mm * s_r;
                }
            }
        }
        const double p_out_plane = fit_out.ok ? fit_out.a + fit_out.b * path_mm : 0.0;
        const double dp_domain = p_in_plane - p_out_plane;
        // Extended piping run: dp between the run's own end planes (s = ext_in and
        // path - ext_out along the flow path). Inlet: the developed inflow line carried to the
        // run's inlet plane. Outlet: the developed downstream line — fitted over the far half
        // of the final straight (run leg + extension) — extrapolated back to the run's outlet
        // plane, the way a fitting's K (with its downstream recovery) is measured.
        const double s_run_in = ext_in, s_run_out = path_mm - ext_out;
        double p_run_in = p_in_plane, p_run_out = p_out_plane;
        if (ext_in > 0.0) {
            if (std::isfinite(developed_grad_pa_mm)) {
                p_run_in = p_in_plane - developed_grad_pa_mm * s_run_in;
            } else {
                const LineFit at = window_fit(s_run_in - 1.5 * v, s_run_in + 1.5 * v);
                if (at.ok) p_run_in = at.a + at.b * s_run_in;
            }
        }
        if (ext_out > 0.0) {
            const double tail = ext_out + std::max(0.0, run_out_straight_mm);
            LineFit down = window_fit(path_mm - 0.5 * tail, path_mm - 2.0 * v);
            if (!down.ok) down = window_fit(s_run_out - 1.5 * v, s_run_out + 1.5 * v);
            if (down.ok) p_run_out = down.a + down.b * s_run_out;
        }
        const double dp_pa = p_run_in - p_run_out;
        const double run_path_mm = s_run_out - s_run_in;
        // Profile along the run (s from the run's inlet plane, gauge to its outlet plane); an
        // extended run's legs show as s < 0 and s > path_length_mm.
        json profile = json::array();
        profile.push_back({{"s_mm", 0.0 - s_run_in}, {"p_pa", p_in_plane - p_run_out}});
        {
            const size_t stride = std::max<size_t>(1, (ps.size() + 199) / 200);
            for (size_t k = 0; k < ps.size(); k += stride) {
                double s = 0, p = 0, w = 0;
                for (size_t j = k; j < std::min(ps.size(), k + stride); ++j) {
                    s += ps[j] * pw[j];
                    p += pp[j] * pw[j];
                    w += pw[j];
                }
                profile.push_back({{"s_mm", s / w - s_run_in}, {"p_pa", p / w - p_run_out}});
            }
        }
        profile.push_back({{"s_mm", path_mm - s_run_in}, {"p_pa", p_out_plane - p_run_out}});
        if (ext_in > 0.0 || ext_out > 0.0)
            caveats.push_back(
                "The run's straight end legs were extended for the solve (inlet +" +
                fmt("%.0f", ext_in) + " mm, outlet +" + fmt("%.0f", ext_out) + " mm, to " +
                fmt("%.0f", kEndLegDiameters) +
                " D) so the inflow is developed and the bend wake clears the outlet boundary. "
                "dp_pa is between the run's own end planes, the outlet pressure taken from the "
                "developed downstream gradient extrapolated back to the run end (the convention "
                "standard K-factors use, so it includes the downstream recovery loss); "
                "dp_domain_pa (" + fmt("%.4g", dp_domain) + " Pa) spans the extended domain.");

        // Darcy straight-pipe friction estimate over the same path for comparison.
        const double f_darcy = darcy_friction_factor(re_phys, roughness_mm / dh_mm);
        const double darcy_pa =
            f_darcy * (run_path_mm / dh_mm) * 0.5 * rho_phys * u_phys * u_phys;

        // ── Caveats / warnings ───────────────────────────────────────────────────────────
        const char* collision_name = cfg.collision == Collision::Rans
                                         ? "regularized BGK + RANS"
                                         : (cfg.collision == Collision::Trt ? "TRT"
                                                                            : "regularized BGK");
        caveats.insert(caveats.begin(),
                       "Internal D3Q19 LBM (incompressible " + std::string(collision_name) +
                           ", half-way bounce-back staircase walls) at " + fmt("%.3g", v) +
                           " mm voxels: the hydraulic diameter spans " + fmt("%.1f", d_lat) +
                           " voxels; walls are hydraulically smooth.");
        const std::string model = turbulent ? "mixing_length+wall_function" : "laminar";
        if (turbulent) {
            caveats.push_back(
                "Turbulence is modelled (steady RANS): mixing-length eddy viscosity (kappa y "
                "with van Driest damping, capped at 0.09 D_h/2) over Reichardt's pipe "
                "distribution, and a Spalding law-of-the-wall stress (lattice coefficient " +
                fmt("%.2g", kWallStressCoef) +
                ", validated to ~10% against Colebrook for Re 1e4-1e6 at 10-20 cells across "
                "D_h) on the wall cells in place of the bounce-back drag. Walls are "
                "hydraulically smooth; separation and secondary-flow losses in fittings are "
                "approximate (compare one_d_estimate_pa when piping + run_id are given, else "
                "darcy_estimate_pa, which is straight-pipe friction only).");
            if (std::isfinite(developed_grad_pa_mm))
                caveats.push_back(
                    "Inflow is fully developed (recycled from the straight inlet leg); the "
                    "inlet-plane pressure is extrapolated along that leg's developed gradient (" +
                    fmt("%.4g", developed_grad_pa_mm * 1000.0) +
                    " Pa/m from the wall-function stress), so no entrance-development loss is "
                    "counted.");
            else if (profile_used != "uniform")
                caveats.push_back("The inlet leg is too short to recycle a developed profile; dp "
                                  "includes the model's entrance adjustment from the power-law "
                                  "inflow.");
            if (re_phys <= 4000.0)
                caveats.push_back("Re " + fmt("%.0f", re_phys) +
                                  " is transitional; the model assumes fully turbulent flow.");
            if (S.yplus_mean < 1.0 || S.yplus_mean > 2500.0) {
                warnings.push_back("wall_function_out_of_range");
                caveats.push_back(
                    "Mean first-cell y+ is " + fmt("%.1f", S.yplus_mean) +
                    ", outside the 1-2500 range the wall function was validated over; " +
                    (S.yplus_mean > 2500.0 ? "lower voxel_mm / raise cells_across."
                                           : "raise voxel_mm / lower cells_across."));
            }
        }
        if (!turbulent && d_lat < 8.0) {
            warnings.push_back("coarse_cross_section");
            caveats.push_back("Only " + fmt("%.1f", d_lat) +
                              " voxels span the hydraulic diameter; laminar dp error can exceed "
                              "10% below ~12 — lower voxel_mm or raise cells_across.");
        } else if (turbulent && (d_lat < 9.0 || d_lat > 22.0)) {
            warnings.push_back("wall_function_resolution");
            caveats.push_back(fmt("%.1f", d_lat) +
                              " voxels span the hydraulic diameter, outside the 10-20 the wall "
                              "function was validated at; set cells_across in that range.");
        }
        if (profile_used == "uniform")
            caveats.push_back(turbulent ? "Uniform inlet profile: dp includes the turbulent "
                                          "entrance-region loss."
                                        : "Uniform inlet profile: dp includes the laminar "
                                          "entrance-region loss (~1.25 dynamic heads).");
        if (!S.converged) {
            warnings.push_back("not_converged");
            caveats.push_back("Stopped at max_steps (" + std::to_string(S.steps) +
                              ") before the pressure drop settled; raise max_steps.");
        }
        if (mass_imbalance > 0.02) {
            warnings.push_back("mass_imbalance");
            caveats.push_back("Inlet/outlet lattice flux differs by " +
                              fmt("%.1f", 100.0 * mass_imbalance) + "%.");
        }
        if (S.max_drho > 0.1) {
            warnings.push_back("compressibility");
            caveats.push_back("Lattice density varied by " + fmt("%.1f", 100.0 * S.max_drho) +
                              "%; weakly-compressible error may be significant.");
        }
        {
            const double axis_cos =
                std::max({std::abs(outlet.n.x), std::abs(outlet.n.y), std::abs(outlet.n.z)});
            if (axis_cos < std::cos(2.0 * 3.14159265358979323846 / 180.0)) {
                warnings.push_back("oblique_outlet");
                caveats.push_back(
                    "The outlet plane is " +
                    fmt("%.0f", std::acos(std::min(1.0, axis_cos)) * 180.0 / 3.14159265358979323846) +
                    " deg off the lattice axes (the inlet axis defines the lattice), so the outlet "
                    "is a staircase and adds a local loss; place the outlet on a straight leg "
                    "parallel or perpendicular to the inlet for the cleanest dp.");
            }
        }

        // Bends: the measured over-prediction band of the bend K at the resolution used.
        json bend_band = nullptr;
        if (bent && turbulent) {
            const BendBand bb = bend_k_band(d_lat);
            bend_band = {{"cells_across", d_lat},
                         {"k_ratio_min", bb.lo},
                         {"k_ratio_max", bb.hi},
                         {"reference", "90-degree long-radius bend K vs Rennels, Re 5e4, "
                                       "measured at " + std::string(bb.measured_at) +
                                       " cells across"}};
            caveats.push_back(
                "The flow path bends: at " + fmt("%.1f", d_lat) +
                " voxels across D_h the model over-predicts a 90-degree long-radius bend's loss "
                "coefficient by " + fmt("%.2f", bb.lo) + "-" + fmt("%.2f", bb.hi) +
                "x Rennels (measured at " + bb.measured_at + " cells across" +
                (d_lat < 15.0 ? "; 16 cells across measured 1.47-1.78x" : "") +
                "), so bend-dominated dp reads high by up to that factor.");
        }

        // 1-D comparison for a piping run: Darcy friction + fitting K at the solved flow.
        json one_d = nullptr;
        if (has_piping) {
            one_d = one_d_fn(params["piping"], run_id, rho_phys, mu_phys, u_phys * area_m2,
                             roughness_mm);
            if (!one_d.value("ok", false)) {
                warnings.push_back("one_d_unavailable");
                caveats.push_back("No 1-D estimate: " + one_d.value("hint", std::string()));
                one_d = nullptr;
            } else if (one_d.value("material_roughness_mm", 0.0) >
                       one_d.value("roughness_mm", 0.0) + 1e-9) {
                caveats.push_back(
                    "one_d_estimate_pa uses roughness_mm " +
                    fmt("%.3g", one_d.value("roughness_mm", 0.0)) +
                    " like the lattice walls; the run's material is " +
                    fmt("%.3g", one_d.value("material_roughness_mm", 0.0)) +
                    " mm (piping_solve uses it) — pass roughness_mm for the commercial-pipe "
                    "1-D figure.");
            }
        }
        std::string hint =
            std::string(ext_in > 0.0 || ext_out > 0.0
                            ? "dp_pa is the pressure drop between the run's own end planes "
                              "(end legs extended for the solve; dp_domain_pa spans the domain)"
                            : "dp_pa is the pressure drop between the inlet and outlet planes") +
            std::string(turbulent ? " (RANS mixing-length + wall-function model)" : "");
        if (!one_d.is_null()) {
            const double est = one_d["total_pa"].get<double>();
            hint += "; the 1-D estimate for run " + run_id + " is " + fmt("%.4g", est) +
                    " Pa (straight friction " + fmt("%.4g", one_d["friction_pa"].get<double>()) +
                    " Pa + " + std::to_string(one_d["fittings"].size()) + " fitting(s) " +
                    fmt("%.4g", one_d["fittings_pa"].get<double>()) + " Pa), CFD/1-D = " +
                    fmt("%.2f", dp_pa / est);
        } else {
            hint += "; darcy_estimate_pa is straight-pipe friction only";
            if (bent) hint += " — bends and fittings add to it; pass piping + run_id for the "
                              "fittings-aware 1-D estimate";
        }

        json out{
            {"ok", true},
            {"dp_pa", dp_pa},
            {"mean_velocity_ms", u_phys},
            {"flow_m3s", u_phys * area_m2},
            {"reynolds", re_phys},
            {"simulated_reynolds", re_sim},
            {"model", model},
            {"regime", regime},
            {"hydraulic_diameter_mm", dh_mm},
            {"inlet_area_mm2", area_mm2},
            {"path_length_mm", run_path_mm},
            {"mass_imbalance", mass_imbalance},
            {"converged", S.converged},
            {"steps", S.steps},
            {"voxels", N},
            {"pressure_profile", profile},
            {"darcy_estimate_pa", darcy_pa},
            {"darcy_friction_factor", f_darcy},
            {"fluid",
             {{"name", fluid_name}, {"density_kgm3", rho_phys}, {"viscosity_pas", mu_phys}}},
            {"caveats", caveats},
            {"warnings", warnings},
            {"hint", hint},
            {"next_actions",
             json::array({"persist with sim_study_add {type:'pipe_flow', engine:"
                          "'cpp-lbm-internal', params, results}",
                          "compare against the 1-D network with piping_verify_cfd"})},
            {"provenance",
             {{"engine", "cpp-lbm-internal"},
              {"voxel_mm", v},
              {"grid", {g.nx, g.ny, g.nz}},
              {"collision", cfg.collision == Collision::Rans
                                ? "regularized-incompressible+mixing-length+spalding-wall"
                                : (cfg.collision == Collision::Trt ? "trt-incompressible"
                                                                   : "regularized-incompressible")},
              {"u_lat", u_eff_lat},
              {"tau", tau_mol},
              {"nu_lat", nu_lat},
              {"y_plus_first_cell",
               turbulent ? json{{"mean", S.yplus_mean}, {"min", S.yplus_min}, {"max", S.yplus_max}}
                         : json(nullptr)},
              {"wall_cells", L.wall_node.size()},
              {"inlet_leg_gradient_pa_per_m",
               std::isfinite(developed_grad_pa_mm) ? json(developed_grad_pa_mm * 1000.0)
                                                   : json(nullptr)},
              {"inlet_profile", inflow},
              {"inlet_voxels", L.n_in},
              {"outlet_voxels", L.n_out},
              {"threads", threads},
              {"inlet_normal", {to_world(inlet.n).x, to_world(inlet.n).y, to_world(inlet.n).z}},
              {"outlet_normal",
               {to_world(outlet.n).x, to_world(outlet.n).y, to_world(outlet.n).z}}}},
        };
        out["bends_detected"] = bent;
        if (ext_in > 0.0 || ext_out > 0.0) {
            out["dp_domain_pa"] = dp_domain;
            out["extended_legs"] = {{"inlet_mm", ext_in},
                                    {"outlet_mm", ext_out},
                                    {"domain_path_length_mm", path_mm}};
        }
        out["provenance"]["cells_across_default"] =
            cells_given || voxel_req > 0.0 ? json(nullptr)
                                           : json(bend_default ? "bend" : "straight");
        if (!bend_band.is_null()) out["bend_loss_band"] = bend_band;
        if (!one_d.is_null()) {
            const double est = one_d["total_pa"].get<double>();
            out["one_d_estimate_pa"] = est;
            out["cfd_over_one_d"] = est > 0.0 ? json(dp_pa / est) : json(nullptr);
            json bd = {{"friction_pa", one_d["friction_pa"]},
                       {"fittings", one_d["fittings"]}};
            for (const char* k : {"fittings_pa", "fittings_K", "friction_factor", "reynolds",
                                  "velocity_ms", "dynamic_pressure_pa", "inner_dia_mm",
                                  "centreline_length_mm",
                                  "bend_radius_mm", "roughness_mm", "material_roughness_mm"})
                bd[k] = one_d[k];
            bd["run_id"] = run_id;
            out["one_d_breakdown"] = std::move(bd);
        }
        return out;
    } catch (const std::bad_alloc&) {
        return refuse("insufficient_memory",
                      "the pipe-flow lattice did not fit in memory; raise voxel_mm");
    } catch (const json::exception& e) {
        return refuse("invalid_params", std::string("invalid parameter type: ") + e.what());
    } catch (const std::exception& e) {
        return refuse("pipe_flow_error", e.what());
    }
}

json run_pipe_flow_stl(const json& params, PipeRunLossFn one_d_fn) {
    const std::string path = params.value("stl_path", std::string());
    if (path.empty()) return refuse("invalid_params", "stl_path is required");
    PipeFlowShell s;
    std::string err;
    if (!load_stl_soup(path, s.pos, s.idx, err)) return refuse("invalid_stl", err);
    return run_pipe_flow({std::move(s)}, params, one_d_fn);
}

}  // namespace chudware::sim
