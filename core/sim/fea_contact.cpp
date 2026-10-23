// Contact-candidate construction — see fea_contact.h.

#include "sim/fea_contact.h"

#include <algorithm>
#include <cstdio>
#include <cmath>
#include <map>
#include <set>
#include <unordered_map>
#include <vector>

namespace chudware::sim {

namespace {

// Boundary-face corner tables. Same node conventions as the mesher / fea_run: tet
// faces reference corners 0..3 (with the tet10 mid-edge nodes of each face listed
// separately), hex faces reference corners 0..7.
constexpr int kTetFaceCorners[4][3] = {{0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
// Mid-edge local node per face edge, mesher edge order {0,1}=4 {1,2}=5 {2,0}=6
// {3,0}=7 {3,2}=8 {3,1}=9.
constexpr int kTetFaceMids[4][3] = {{4, 5, 6}, {4, 9, 7}, {6, 8, 7}, {5, 8, 9}};
constexpr int kHexFaceCorners[6][4] = {{0, 1, 2, 3}, {4, 5, 6, 7}, {0, 1, 5, 4},
                                       {2, 3, 7, 6}, {0, 3, 7, 4}, {1, 2, 6, 5}};

// One boundary DIRECTION at a surface node: the (area-weighted mean) outward normal of
// the incident boundary faces that point the same way, and their tributary area.
//
// A node in the MIDDLE of a flat interface has exactly one of these. A node on the EDGE
// of that interface also belongs to the side walls, so it has two or three — and its
// area-weighted average normal points nowhere useful (measured 0.8 in z at the rim of a
// stacked-box interface, tilting every gap it produced). Contact is therefore matched
// per direction, not per node: a pair is formed between the two facing directions.
struct NodeDir {
    double n[3] = {0.0, 0.0, 0.0};
    double area = 0.0;
};

struct UnionFind {
    std::vector<int> parent;
    explicit UnionFind(int n) : parent(static_cast<std::size_t>(n)) {
        for (int i = 0; i < n; ++i) parent[static_cast<std::size_t>(i)] = i;
    }
    int find(int x) {
        while (parent[static_cast<std::size_t>(x)] != x) {
            parent[static_cast<std::size_t>(x)] =
                parent[static_cast<std::size_t>(parent[static_cast<std::size_t>(x)])];
            x = parent[static_cast<std::size_t>(x)];
        }
        return x;
    }
    void unite(int a, int b) {
        a = find(a);
        b = find(b);
        if (a != b) parent[static_cast<std::size_t>(std::max(a, b))] = std::min(a, b);
    }
};

// Faces within ~11 degrees of each other are one direction (cos 11 deg ~ 0.98): fine
// enough to keep a cylinder's facets distinct at 16 divisions, coarse enough that a flat
// interface stays a single direction whatever its triangulation.
constexpr double kSameDirection = 0.98;

void add_direction(std::vector<NodeDir>& dirs, const double n[3], double area) {
    for (NodeDir& d : dirs) {
        const double len = std::sqrt(d.n[0] * d.n[0] + d.n[1] * d.n[1] + d.n[2] * d.n[2]);
        if (len <= 0.0) continue;
        const double dot = (d.n[0] * n[0] + d.n[1] * n[1] + d.n[2] * n[2]) / len;
        if (dot >= kSameDirection) {
            for (int c = 0; c < 3; ++c) d.n[c] += area * n[c];
            d.area += area;
            return;
        }
    }
    NodeDir fresh;
    for (int c = 0; c < 3; ++c) fresh.n[c] = area * n[c];
    fresh.area = area;
    dirs.push_back(fresh);
}

}  // namespace

ContactCandidateSet fea_contact_candidates(const std::vector<double>& points,
                                           const std::vector<int>& tets,
                                           int nodes_per_elem, double E_mpa,
                                           const ContactCandidateOptions& options) {
    ContactCandidateSet out;
    const int n_nodes = static_cast<int>(points.size() / 3);
    if (nodes_per_elem != 4 && nodes_per_elem != 10 && nodes_per_elem != 8) {
        out.error = "contact: nodes_per_elem must be 4, 10 or 8";
        return out;
    }
    if (n_nodes == 0 || tets.empty() ||
        tets.size() % static_cast<std::size_t>(nodes_per_elem) != 0) {
        out.error = "contact: empty or ragged mesh";
        return out;
    }
    if (!(options.max_gap_mm > 0.0) || !std::isfinite(options.max_gap_mm)) {
        out.error = "contact: max_gap_mm must be finite and > 0";
        return out;
    }
    if (!(options.penalty_scale > 0.0) || !std::isfinite(options.penalty_scale)) {
        out.error = "contact: penalty_scale must be finite and > 0";
        return out;
    }
    if (!(options.min_opposition > 0.0) || options.min_opposition > 1.0) {
        out.error = "contact: min_opposition must be in (0, 1]";
        return out;
    }
    if (!(E_mpa > 0.0) || !std::isfinite(E_mpa)) {
        out.error = "contact: material E must be finite and > 0";
        return out;
    }
    const int stride = nodes_per_elem;
    const int n_el = static_cast<int>(tets.size()) / stride;
    for (int nid : tets) {
        if (nid < 0 || nid >= n_nodes) {
            out.error = "contact: element node index out of range";
            return out;
        }
    }

    // ── 1. Component labelling (one per disconnected solid body) ─────────────────
    UnionFind uf(n_nodes);
    std::vector<char> meshed(static_cast<std::size_t>(n_nodes), 0);
    for (int e = 0; e < n_el; ++e) {
        const int* el = &tets[static_cast<std::size_t>(e) * stride];
        for (int j = 0; j < stride; ++j) meshed[static_cast<std::size_t>(el[j])] = 1;
        for (int j = 1; j < stride; ++j) uf.unite(el[0], el[j]);
    }
    out.component.assign(static_cast<std::size_t>(n_nodes), -1);
    std::map<int, int> root_ordinal;
    for (int i = 0; i < n_nodes; ++i) {
        if (!meshed[static_cast<std::size_t>(i)]) continue;
        const int root = uf.find(i);
        auto it = root_ordinal.find(root);
        if (it == root_ordinal.end())
            it = root_ordinal.emplace(root, static_cast<int>(root_ordinal.size())).first;
        out.component[static_cast<std::size_t>(i)] = it->second;
    }
    out.n_components = static_cast<int>(root_ordinal.size());
    auto ordinal = [&](int node) {
        return options.node_body_ordinal ? options.node_body_ordinal(node)
                                         : out.component[static_cast<std::size_t>(node)] + 1;
    };
    std::set<int> body_ordinals;
    if (!options.bonded_pairs.empty() || !options.contact_pairs.empty())
        for (int i = 0; i < n_nodes; ++i)
            if (meshed[static_cast<std::size_t>(i)]) body_ordinals.insert(ordinal(i));
    std::map<std::array<int, 2>, InterfaceKind> interfaces;
    auto declare = [&](const std::vector<std::array<int, 2>>& pairs, InterfaceKind kind) {
        for (auto pair : pairs) {
            std::sort(pair.begin(), pair.end());
            for (int body : pair)
                if (body <= 0 || !body_ordinals.count(body)) {
                    out.error = "interfaces: body ordinal " + std::to_string(body) + " is not in the mesh";
                    return false;
                }
            if (pair[0] == pair[1]) {
                out.error = "interfaces: body ordinal " + std::to_string(pair[0]) + " cannot interface with itself";
                return false;
            }
            const auto previous = interfaces.find(pair);
            if (previous != interfaces.end() && previous->second != kind) {
                out.error = "interfaces: pair [" + std::to_string(pair[0]) + "," +
                            std::to_string(pair[1]) + "] is listed in both bonded and contact";
                return false;
            }
            interfaces[pair] = kind;
        }
        return true;
    };
    if (!declare(options.bonded_pairs, InterfaceKind::Bonded) ||
        !declare(options.contact_pairs, InterfaceKind::Contact)) return out;
    auto kind_of = [&](int a, int b) {
        const int oa = ordinal(a), ob = ordinal(b);
        if (oa == ob) return InterfaceKind::Free;
        const auto it = interfaces.find({std::min(oa, ob), std::max(oa, ob)});
        return it == interfaces.end() ? options.default_interface : it->second;
    };

    // Characteristic element length: mean first-edge length over a sample of elements.
    {
        double h_sum = 0.0;
        int h_n = 0;
        const int step = std::max(1, n_el / 1000);
        for (int e = 0; e < n_el; e += step) {
            const int a = tets[static_cast<std::size_t>(e) * stride];
            const int b = tets[static_cast<std::size_t>(e) * stride + 1];
            const double dx = points[a * 3] - points[b * 3];
            const double dy = points[a * 3 + 1] - points[b * 3 + 1];
            const double dz = points[a * 3 + 2] - points[b * 3 + 2];
            h_sum += std::sqrt(dx * dx + dy * dy + dz * dz);
            ++h_n;
        }
        out.char_len_mm = (h_n > 0 && h_sum > 0.0) ? (h_sum / h_n) : 1.0;
    }

    // ── 2. Mesh boundary faces -> per-node surface directions + tributary areas ──
    // A face owned by exactly one element is on the boundary. Its raw fan normal can
    // point INTO the element (the corner tables are not uniformly CCW-from-outside),
    // so it is flipped to point away from the element centroid.
    const int n_faces = (stride == 8) ? 6 : 4;
    const int fk = (stride == 8) ? 4 : 3;
    auto face_key = [&](const int* el, const int* fc) {
        std::array<int, 4> key{-1, -1, -1, -1};
        for (int i = 0; i < fk; ++i) key[i] = el[fc[i]];
        std::sort(key.begin(), key.begin() + fk);
        return key;
    };
    std::map<std::array<int, 4>, int> face_owners;
    for (int e = 0; e < n_el; ++e) {
        const int* el = &tets[static_cast<std::size_t>(e) * stride];
        for (int f = 0; f < n_faces; ++f) {
            const int* fc = (stride == 8) ? kHexFaceCorners[f] : kTetFaceCorners[f];
            ++face_owners[face_key(el, fc)];
        }
    }
    std::vector<std::vector<NodeDir>> node_dirs(static_cast<std::size_t>(n_nodes));
    // Local SURFACE element size per node: the mean of the (corner) edge lengths of
    // the boundary faces incident on it. This is the length that decides whether a
    // clearance is resolved — not the study's background size, which on a locally
    // refined interface is a different number entirely. For tet10 the mid-edge nodes
    // inherit their face's corner-edge length: the ELEMENT edge is what resolves a
    // feature, and a mid node placed on it does not make the element smaller.
    std::vector<double> node_surface_h(static_cast<std::size_t>(n_nodes), 0.0);
    std::vector<int> node_surface_faces(static_cast<std::size_t>(n_nodes), 0);
    for (int e = 0; e < n_el; ++e) {
        const int* el = &tets[static_cast<std::size_t>(e) * stride];
        for (int f = 0; f < n_faces; ++f) {
            const int* fc = (stride == 8) ? kHexFaceCorners[f] : kTetFaceCorners[f];
            const auto own = face_owners.find(face_key(el, fc));
            if (own == face_owners.end() || own->second != 1) continue;
            int cn[4];
            for (int i = 0; i < fk; ++i) cn[i] = el[fc[i]];
            const double* p0 = &points[static_cast<std::size_t>(cn[0]) * 3];
            double nvx = 0.0, nvy = 0.0, nvz = 0.0;
            for (int i = 1; i < fk - 1; ++i) {
                const double* pa = &points[static_cast<std::size_t>(cn[i]) * 3];
                const double* pb = &points[static_cast<std::size_t>(cn[i + 1]) * 3];
                const double ax = pa[0] - p0[0], ay = pa[1] - p0[1], az = pa[2] - p0[2];
                const double bx = pb[0] - p0[0], by = pb[1] - p0[1], bz = pb[2] - p0[2];
                nvx += 0.5 * (ay * bz - az * by);
                nvy += 0.5 * (az * bx - ax * bz);
                nvz += 0.5 * (ax * by - ay * bx);
            }
            const double area = std::sqrt(nvx * nvx + nvy * nvy + nvz * nvz);
            if (!(area > 1e-14)) continue;
            double normal[3] = {nvx / area, nvy / area, nvz / area};
            {
                const int nc = (stride == 8) ? 8 : 4;
                double centroid[3] = {0.0, 0.0, 0.0}, face_c[3] = {0.0, 0.0, 0.0};
                for (int i = 0; i < nc; ++i)
                    for (int c = 0; c < 3; ++c)
                        centroid[c] += points[static_cast<std::size_t>(el[i]) * 3 + c] / nc;
                for (int i = 0; i < fk; ++i)
                    for (int c = 0; c < 3; ++c)
                        face_c[c] += points[static_cast<std::size_t>(cn[i]) * 3 + c] / fk;
                double outward = 0.0;
                for (int c = 0; c < 3; ++c) outward += normal[c] * (face_c[c] - centroid[c]);
                if (outward < 0.0)
                    for (int c = 0; c < 3; ++c) normal[c] = -normal[c];
            }
            // Every node ON the face carries part of it: for tet10 that includes the
            // mid-edge nodes, or half the interface would have no contact spring and
            // the load path would be a checkerboard.
            int fn[6];
            int n_fn = fk;
            for (int i = 0; i < fk; ++i) fn[i] = cn[i];
            if (stride == 10) {
                for (int i = 0; i < 3; ++i) fn[3 + i] = el[kTetFaceMids[f][i]];
                n_fn = 6;
            }
            const double share = area / static_cast<double>(n_fn);
            double edge_sum = 0.0;
            for (int i = 0; i < fk; ++i) {
                const double* pa = &points[static_cast<std::size_t>(cn[i]) * 3];
                const double* pb = &points[static_cast<std::size_t>(cn[(i + 1) % fk]) * 3];
                const double dx = pa[0] - pb[0], dy = pa[1] - pb[1], dz = pa[2] - pb[2];
                edge_sum += std::sqrt(dx * dx + dy * dy + dz * dz);
            }
            const double face_h = edge_sum / static_cast<double>(fk);
            for (int i = 0; i < n_fn; ++i) {
                add_direction(node_dirs[static_cast<std::size_t>(fn[i])], normal, share);
                node_surface_h[static_cast<std::size_t>(fn[i])] += face_h;
                ++node_surface_faces[static_cast<std::size_t>(fn[i])];
            }
        }
    }
    std::vector<int> surface;
    for (int i = 0; i < n_nodes; ++i) {
        std::vector<NodeDir>& dirs = node_dirs[static_cast<std::size_t>(i)];
        for (auto it = dirs.begin(); it != dirs.end();) {
            const double len =
                std::sqrt(it->n[0] * it->n[0] + it->n[1] * it->n[1] + it->n[2] * it->n[2]);
            if (!(len > 1e-12) || !(it->area > 0.0)) {
                it = dirs.erase(it);
                continue;
            }
            for (int c = 0; c < 3; ++c) it->n[c] /= len;
            ++it;
        }
        if (!dirs.empty()) surface.push_back(i);
    }
    out.surface_nodes = static_cast<int>(surface.size());
    if (surface.empty()) {
        out.error = "contact: the mesh has no usable boundary faces";
        return out;
    }
    if (out.n_components < 2) return out;  // one body: no candidate pairs, not an error

    // ── 3. Pair facing surface directions on different components ───────────────
    // The search radius is wider than max_gap_mm: two conforming interface meshes are
    // separated by max_gap_mm along the NORMAL but their nodes are a node spacing apart
    // LATERALLY, so a plain 3D max_gap_mm ball would find nothing on any real mesh. A
    // candidate must satisfy both separately: |gap| <= max_gap_mm along n̂, and a lateral
    // offset within one element.
    const double lateral_tol = out.char_len_mm;
    const double radius = options.max_gap_mm + lateral_tol;
    const double cell = std::max(radius, 1e-6);
    auto cell_key = [&](double x, double y, double z) {
        const long long ix = static_cast<long long>(std::floor(x / cell));
        const long long iy = static_cast<long long>(std::floor(y / cell));
        const long long iz = static_cast<long long>(std::floor(z / cell));
        return (ix * 73856093LL) ^ (iy * 19349663LL) ^ (iz * 83492791LL);
    };
    std::unordered_map<long long, std::vector<int>> grid;
    grid.reserve(surface.size());
    for (int i : surface)
        grid[cell_key(points[i * 3], points[i * 3 + 1], points[i * 3 + 2])].push_back(i);

    std::map<std::array<int, 2>, int> group_of;
    std::map<std::array<int, 2>, char> paired;  // (min node, max node) dedupe
    struct Best {
        double score = 0.0;
        int node = -1;
        int dir = -1;
        double gap = 0.0;
    };
    // The facet gap of every spring, kept in step with out.springs: the spring
    // itself carries the gap the PHYSICS uses (analytic where the probe answered),
    // and the facet value is what the resolution block quotes beside it.
    std::vector<double> facet_gaps;
    std::vector<bool> analytic_gaps;
    std::set<int> fallback_nodes;
    for (int i : surface) {
        const int ci = out.component[static_cast<std::size_t>(i)];
        const double pi[3] = {points[i * 3], points[i * 3 + 1], points[i * 3 + 2]};
        const std::vector<NodeDir>& dirs_i = node_dirs[static_cast<std::size_t>(i)];
        // Best (most directly facing) partner per other component.
        std::map<int, Best> best;
        for (int ox = -1; ox <= 1; ++ox)
            for (int oy = -1; oy <= 1; ++oy)
                for (int oz = -1; oz <= 1; ++oz) {
                    const auto it = grid.find(
                        cell_key(pi[0] + ox * cell, pi[1] + oy * cell, pi[2] + oz * cell));
                    if (it == grid.end()) continue;
                    for (int j : it->second) {
                        const int cj = out.component[static_cast<std::size_t>(j)];
                        if (cj == ci) continue;
                        if (kind_of(i, j) == InterfaceKind::Free) continue;
                        double d[3];
                        double d2 = 0.0;
                        for (int c = 0; c < 3; ++c) {
                            d[c] = points[static_cast<std::size_t>(j) * 3 + c] - pi[c];
                            d2 += d[c] * d[c];
                        }
                        if (d2 > radius * radius) continue;
                        const std::vector<NodeDir>& dirs_j =
                            node_dirs[static_cast<std::size_t>(j)];
                        for (std::size_t a = 0; a < dirs_i.size(); ++a) {
                            const double* na = dirs_i[a].n;
                            double gap = 0.0;
                            for (int c = 0; c < 3; ++c) gap += na[c] * d[c];
                            if (std::abs(gap) > options.max_gap_mm) continue;
                            const double lat2 = std::max(0.0, d2 - gap * gap);
                            if (lat2 > lateral_tol * lateral_tol) continue;
                            bool facing = false;
                            for (const NodeDir& dj : dirs_j) {
                                double opp = 0.0;
                                for (int c = 0; c < 3; ++c) opp += na[c] * dj.n[c];
                                if (opp <= -options.min_opposition) {
                                    facing = true;
                                    break;
                                }
                            }
                            if (!facing) continue;
                            // Prefer the most directly-opposed node (smallest lateral
                            // offset), then the smallest gap.
                            const double score = lat2 + gap * gap;
                            auto bit = best.find(cj);
                            if (bit == best.end() || score < bit->second.score)
                                best[cj] = Best{score, j, static_cast<int>(a), gap};
                        }
                    }
                }
        for (const auto& kv : best) {
            const Best& b = kv.second;
            const std::array<int, 2> dedupe{std::min(i, b.node), std::max(i, b.node)};
            if (!paired.emplace(dedupe, 1).second) continue;
            const int cj = out.component[static_cast<std::size_t>(b.node)];
            // ── The gap the physics uses: the CAD surfaces', not the facets' ────
            // The PAIRING stays a mesh question (which nodes face each other, and
            // how far apart the mesh put them), because that is what the springs
            // are built on. The GAP is a geometry question, and on a curved fit
            // the node-to-node projection answers it wrongly by up to the whole
            // clearance — see ContactGapProbe in fea_contact.h.
            double gap_used = b.gap;
            bool measured_on_cad = false;
            const bool bonded = kind_of(i, b.node) == InterfaceKind::Bonded;
            if (options.gap_probe != nullptr) {
                double analytic = 0.0;
                const double* pj = &points[static_cast<std::size_t>(b.node) * 3];
                if (options.gap_probe->pair_gap_mm(i, pi, b.node, pj, analytic)) {
                    gap_used = analytic;
                    measured_on_cad = true;
                    if (!bonded) ++out.gap_analytic_pairs;
                } else {
                    if (!bonded) {
                        fallback_nodes.insert(i);
                        fallback_nodes.insert(b.node);
                    }
                }
            }
            // max_gap_mm is the caller's statement of what counts as contact, and
            // it was applied to a facet gap that had understated the clearance. A
            // pair the true surfaces put further apart than that is not a
            // candidate at all.
            if (std::abs(gap_used) > options.max_gap_mm) {
                ++out.gap_beyond_max_pairs;
                continue;
            }
            if (bonded && options.bond_gap_mm >= 0.0 &&
                std::abs(gap_used) > options.bond_gap_mm + 1e-8) continue;
            const std::array<int, 2> comps{std::min(ci, cj), std::max(ci, cj)};
            auto git = group_of.find(comps);
            if (!bonded && git == group_of.end()) {
                git = group_of.emplace(comps, static_cast<int>(out.group_components.size()))
                          .first;
                out.group_components.push_back(comps);
            }
            // Orient every pair in a group the SAME way — node `a` on the lower
            // component ordinal, the normal pointing at the higher one — so the group's
            // summed resultant means "component cmin pushes cmax away" instead of
            // cancelling pairs that happened to be discovered from the other side.
            // gap0 = n̂·(p_b - p_a) is invariant under (a<->b, n̂ -> -n̂), so the flip
            // changes no physics.
            const NodeDir& dir = dirs_i[static_cast<std::size_t>(b.dir)];
            const bool flip = ci > cj;
            ContactSpring s;
            s.a = flip ? b.node : i;
            s.b = flip ? i : b.node;
            const double pair_E = options.pair_modulus ? options.pair_modulus(s.a, s.b) : E_mpa;
            if (!std::isfinite(pair_E) || pair_E <= 0.0) {
                out.error = "contact: pair material E must be finite and > 0";
                return out;
            }
            s.k = pair_E * out.char_len_mm * (bonded ? 1.0 : options.penalty_scale);
            s.gap0_mm = gap_used;
            for (int c = 0; c < 3; ++c)
                s.normal[static_cast<std::size_t>(c)] = flip ? -dir.n[c] : dir.n[c];
            s.area_mm2 = dir.area;
            if (bonded) {
                s.group = -1;
                out.bonded.push_back(s);
                continue;
            }
            s.group = git->second;
            out.springs.push_back(s);
            std::array<double, 3> support = s.normal;
            if (options.gap_probe != nullptr) {
                std::array<double, 3> analytic_normal{};
                const double* pj = &points[static_cast<std::size_t>(b.node) * 3];
                if (options.gap_probe->pair_normal(i, pi, b.node, pj, analytic_normal)) {
                    for (int axis = 0; axis < 3; ++axis)
                        support[axis] = flip ? -analytic_normal[axis] : analytic_normal[axis];
                    ++out.support_normal_analytic_pairs;
                }
            }
            out.support_normals.push_back(support);
            facet_gaps.push_back(b.gap);
            analytic_gaps.push_back(measured_on_cad);
        }
    }

    // ── Interface resolution facts (see fea_contact.h ContactResolution) ────────
    // Measured on the candidate set that was just built, so a locally refined
    // interface reports its own refined size and gaps rather than the study's
    // background numbers.
    if (!out.springs.empty()) {
        std::vector<double> h_at_pair;
        std::vector<double> gaps;
        h_at_pair.reserve(out.springs.size());
        gaps.reserve(out.springs.size());
        auto node_h = [&](int nid) {
            const std::size_t at = static_cast<std::size_t>(nid);
            return node_surface_faces[at] > 0
                       ? node_surface_h[at] / static_cast<double>(node_surface_faces[at])
                       : 0.0;
        };
        // Tributary area is a NODE property and the search pairs from both sides,
        // so one node can appear in two pairs: count each node's area once (node
        // `a` is always on the lower component ordinal, so this measures exactly
        // one side of the interface).
        std::set<int> area_counted;
        for (const ContactSpring& s : out.springs) {
            // The COARSER of the two sides governs: it is the side that decides how
            // many pairs land in the closing zone.
            h_at_pair.push_back(std::max(node_h(s.a), node_h(s.b)));
            gaps.push_back(s.gap0_mm);
            if (area_counted.insert(s.a).second) out.candidate_area_mm2 += s.area_mm2;
        }
        auto median_of = [](std::vector<double>& v) {
            const std::size_t mid = v.size() / 2;
            std::nth_element(v.begin(), v.begin() + static_cast<std::ptrdiff_t>(mid),
                             v.end());
            return v[mid];
        };
        out.min_gap0_mm = *std::min_element(gaps.begin(), gaps.end());
        std::vector<double> gaps_copy = gaps;
        out.median_gap0_mm = median_of(gaps_copy);
        out.interface_h_mm = median_of(h_at_pair);
        // The facet statistics, always measured, and their difference from the
        // analytic ones: what the linear discretization of this interface did to
        // the clearance the guard has to judge (ContactGapProbe in fea_contact.h).
        out.min_gap_facet_mm = *std::min_element(facet_gaps.begin(), facet_gaps.end());
        out.median_gap_facet_mm = median_of(facet_gaps);
        out.facet_error_mm = out.min_gap0_mm - out.min_gap_facet_mm;
        // A NEGATIVE mesh gap is placement/facet error, not a press fit: the bodies
        // were placed touching and the discretization pushed nodes through the
        // partner face. k*|gap0| of preload is what that becomes if it is left in the
        // spring — 56 kN through 11 pairs on the live keyway, reported as 3.0e6 MPa
        // for a 1500 N load. The depth is REPORTED instead, and it is also the one
        // direct measurement of this interface's own surface error: two bodies placed
        // touching have no negative true gap, so a penetration of 0.37 mm says the
        // mesh's boundary is that far from the CAD surface here. The RAW closest
        // approach stays in min_gap0_mm — it is what tells the resolution guard the
        // faces touch.
        out.initial_penetration_max_mm = std::max(0.0, -out.min_gap0_mm);
        // "Can seat": the initial gap is at or below what this mesh can resolve AT
        // this interface — a length the interface supplies, never a multiple of a
        // closest approach that may be 0 or negative (see kSeatFractionH). These are
        // the pairs the solve's active set STARTS from; the rest seat as the bodies
        // deform, and any of these that ends tensile is released.
        out.seat_limit_mm = std::max({kSeatFractionH * out.interface_h_mm,
                                      kSeatFractionGap * options.max_gap_mm,
                                      kSeatMinAbsMm});
        out.closed_fit_limit_mm = options.closed_fit_gap_mm >= 0.0
                                      ? options.closed_fit_gap_mm
                                      : out.interface_h_mm * 0.25;
        // Closing a sub-resolution CAD clearance is an explicit modelling mode,
        // never a facet-error correction. Preserve measured gaps above; only the
        // unilateral spring sees zero, so tensile pairs still release normally.
        for (std::size_t i = 0; i < out.springs.size(); ++i) {
            ContactSpring& s = out.springs[i];
            if (options.closed_fit && analytic_gaps[i] &&
                s.gap0_mm >= 0.0 && s.gap0_mm < out.closed_fit_limit_mm) {
                ++out.closed_fit_pairs;
                out.closed_fit_gap_mm_max =
                    std::max(out.closed_fit_gap_mm_max, s.gap0_mm);
                s.gap0_mm = 0.0;
            }
            if (s.gap0_mm < 0.0) s.gap0_mm = 0.0;
            if (s.gap0_mm <= out.seat_limit_mm) ++out.seatable_pairs;
        }
    }
    // Counted whether or not any candidate survived: a set emptied by the analytic
    // measurement still has to say what it could not measure.
    out.gap_fallback_nodes = static_cast<int>(fallback_nodes.size());
    return out;
}

ContactSupportCone fea_contact_support_cone(const ContactCandidateSet& candidates,
                                           int component,
                                           const std::array<double, 3>& applied_force) {
    ContactSupportCone out;
    double magnitude = 0.0;
    for (double force : applied_force) magnitude += force * force;
    magnitude = std::sqrt(magnitude);
    if (!(magnitude > kLoadPathMinAppliedN)) return out;
    out.normal_min.fill(1.0);
    out.normal_max.fill(-1.0);
    for (int axis = 0; axis < 3; ++axis)
        out.load_direction[axis] = applied_force[axis] / magnitude;
    auto sign_on_body = [&](const ContactSpring& spring) {
        const bool a = candidates.component[spring.a] == component;
        const bool b = candidates.component[spring.b] == component;
        return a == b ? 0.0 : (a ? -1.0 : 1.0);
    };
    auto normal_at = [&](std::size_t i) -> const std::array<double, 3>& {
        return candidates.support_normals.size() == candidates.springs.size()
                   ? candidates.support_normals[i] : candidates.springs[i].normal;
    };
    for (std::size_t i = 0; i < candidates.springs.size(); ++i) {
        const auto& spring = candidates.springs[i];
        const double sign = sign_on_body(spring);
        if (sign == 0.0) continue;
        if (out.pairs == 0) out.group = spring.group;
        else if (out.group != spring.group) out.group = -1;
        ++out.pairs;
        for (int axis = 0; axis < 3; ++axis) {
            const double value = sign * normal_at(i)[axis];
            out.normal_min[axis] = std::min(out.normal_min[axis], value);
            out.normal_max[axis] = std::max(out.normal_max[axis], value);
        }
    }
    if (out.pairs == 0) return out;
    for (int axis = 0; axis < 3; ++axis) {
        if ((out.normal_min[axis] > 1e-6 && out.load_direction[axis] >= -1e-8) ||
            (out.normal_max[axis] < -1e-6 && out.load_direction[axis] <= 1e-8)) {
            out.separating_axis = axis;
            out.excludes_load = true;
            break;
        }
    }
    if (!out.excludes_load) return out;
    int transverse = 0;
    for (int axis = 1; axis < 3; ++axis)
        if (std::abs(out.load_direction[axis]) > std::abs(out.load_direction[transverse]))
            transverse = axis;
    if (transverse == out.separating_axis) transverse = (transverse + 1) % 3;
    double minimum_angle = 4.0, maximum_angle = -4.0;
    for (std::size_t i = 0; i < candidates.springs.size(); ++i) {
        const double sign = sign_on_body(candidates.springs[i]);
        if (sign == 0.0) continue;
        const auto& normal = normal_at(i);
        const double angle = std::atan2(sign * normal[transverse],
                                       std::abs(normal[out.separating_axis]));
        minimum_angle = std::min(minimum_angle, angle);
        maximum_angle = std::max(maximum_angle, angle);
    }
    out.supported_arc_deg = (maximum_angle - minimum_angle) * 180.0 / std::acos(-1.0);
    return out;
}

ContactResolution fea_contact_resolution(const ContactCandidateSet& candidates) {
    ContactResolution out;
    out.closed_fit_pairs = candidates.closed_fit_pairs;
    out.closed_fit_gap_mm_max = candidates.closed_fit_gap_mm_max;
    out.closed_fit_limit_mm = candidates.closed_fit_limit_mm;
    out.min_active_pairs = kContactMinActivePairs;
    out.candidate_pairs = static_cast<int>(candidates.springs.size());
    out.seatable_pairs = candidates.seatable_pairs;
    out.seat_limit_mm = candidates.seat_limit_mm;
    out.initial_penetration_max_mm = candidates.initial_penetration_max_mm;
    out.h_interface_mm = candidates.interface_h_mm;
    out.candidate_area_mm2 = candidates.candidate_area_mm2;
    // The clearance the discretization has to resolve is the interface's own closest
    // approach. A set whose closest pair is TOUCHING or interfering (gap <= 0) has no
    // clearance to resolve, so the ratio is undefined and the patch criterion applies
    // instead (fea_contact.h).
    out.gap_mm = candidates.min_gap0_mm > 0.0 ? candidates.min_gap0_mm : 0.0;
    out.gap_facet_mm = candidates.min_gap_facet_mm;
    out.facet_error_mm = candidates.facet_error_mm;
    out.gap_analytic = candidates.gap_analytic_pairs > 0;
    out.gap_fallback_nodes = candidates.gap_fallback_nodes;
    const bool touching = candidates.closed_fit_pairs > 0 || !(out.gap_mm > 0.0);
    out.criterion = touching ? kCriterionPatchArea : kCriterionClearance;
    if (touching) {
        out.max_h_interface_mm = out.candidate_area_mm2 > 0.0
                                     ? std::sqrt(out.candidate_area_mm2) /
                                           kContactMinPatchElems
                                     : 0.0;
    } else {
        out.ratio = out.h_interface_mm / out.gap_mm;
        out.max_ratio = kContactMaxHOverGap;
    }
    // ── The size that resolves this interface ───────────────────────────────────
    // From the GEOMETRY: kContactMaxHOverGap elements' worth of the clearance, or
    // kContactMinPatchElems across the patch when the faces touch. The `h/2` term
    // is a floor for the case where that geometric size is COARSER than the
    // element size already at the interface — an interface that funnelled at a
    // legal ratio has nothing to learn from "use 4 x the clearance", it has to be
    // refined to place more pairs, so halving what it has is the only actionable
    // advice.
    //
    // It does NOT apply when the geometric criterion is the one that failed. Then
    // the geometric size is finer than h by construction (ratio > max_ratio means
    // h > max_ratio*gap) and it is exactly the size that makes the interface
    // legal; halving h on top asks for resolution the criterion does not need. On
    // the rover hinge that difference is the whole answer: a 0.300 mm clearance
    // at h = 1.775 mm needs 1.200 mm, and h/2 = 0.887 mm would have been a demand
    // for twice the resolution the physics asks for on a fit whose mesher is
    // already at its cell-shape floor.
    const double from_geometry = touching ? out.max_h_interface_mm
                                          : kContactMaxHOverGap * out.gap_mm;
    const bool geometry_failed =
        !touching && out.ratio > kContactMaxHOverGap && from_geometry > 0.0;
    out.required_interface_size_mm =
        geometry_failed
            ? from_geometry
            : (out.h_interface_mm > 0.0 && from_geometry > 0.0
                   ? std::min(from_geometry, out.h_interface_mm * 0.5)
                   : (from_geometry > 0.0 ? from_geometry
                                          : out.h_interface_mm * 0.5));
    if (candidates.springs.empty()) return out;
    if (touching) {
        // No clearance: the patch decides. Both halves are required, same shape as
        // the clearance clause — a small patch resolved by fine elements is a real
        // seat, and a big patch with 3 pairs on it is not.
        const bool too_coarse = out.max_h_interface_mm > 0.0 &&
                                out.h_interface_mm > out.max_h_interface_mm;
        if (too_coarse && out.seatable_pairs < kContactMinActivePairs) {
            char text[704];
            std::snprintf(
                text, sizeof(text),
                "the contact interface is not resolved by this mesh: the faces TOUCH "
                "(closest approach 0 mm), so there is no clearance to resolve and the "
                "interface is judged on its patch — the element size AT the interface "
                "is %.4g mm across a %.4g mm^2 candidate patch (%.4g mm wide), which "
                "is fewer than %.0f elements across it, and only %d of %d candidate "
                "pairs sit within the %.4g mm seat limit. So any load would funnel "
                "through those few nodes and be reported as a contact pressure. Set "
                "contact.interface_size_mm to %.3g (mm) or finer: it refines the "
                "candidate faces LOCALLY, so the interface is resolved without "
                "meshing the whole part that fine",
                out.h_interface_mm, out.candidate_area_mm2,
                std::sqrt(out.candidate_area_mm2), kContactMinPatchElems,
                out.seatable_pairs, out.candidate_pairs, out.seat_limit_mm,
                out.required_interface_size_mm);
            out.reason = text;
            out.resolved = false;
        }
        return out;
    }
    if (out.ratio > kContactMaxHOverGap && out.seatable_pairs < kContactMinActivePairs) {
        // Which clearance is being judged is part of the evidence: an ANALYTIC
        // measurement is the CAD surfaces' own clearance and does not move when
        // the mesh does, so the refusal is a statement about the geometry. A
        // facet-only one (no CAD to measure against) can be an artefact of where
        // the two meshes sampled a curved fit, and the reader has to know that.
        char source[224];
        if (out.gap_analytic)
            std::snprintf(source, sizeof(source),
                          "%.4g mm clearance (measured on the CAD faces; the mesh's "
                          "own facets read %.4g mm)",
                          out.gap_mm, out.gap_facet_mm);
        else
            std::snprintf(source, sizeof(source),
                          "%.4g mm clearance (measured on the mesh facets — no CAD "
                          "face was available, so on a curved fit this can understate "
                          "the real clearance)",
                          out.gap_mm);
        char text[832];
        std::snprintf(
            text, sizeof(text),
            "the contact interface is not resolved by this mesh: the element size AT "
            "the interface is %.4g mm against a %s (ratio %.1f, limit "
            "%.0f), and only %d of %d candidate pairs sit within the %.4g mm seat "
            "limit — so any load would funnel through those few nodes and be "
            "reported as a contact pressure. Set contact.interface_size_mm to %.3g "
            "(mm) or finer: it refines the candidate faces LOCALLY, so the interface "
            "is resolved without meshing the whole part that fine",
            out.h_interface_mm, source, out.ratio, kContactMaxHOverGap,
            out.seatable_pairs, out.candidate_pairs, out.seat_limit_mm,
            out.required_interface_size_mm);
        out.reason = text;
        out.resolved = false;
    }
    return out;
}

void fea_contact_resolution_after_solve(ContactResolution& resolution,
                                        int active_pairs, double contact_force_n,
                                        double contact_area_mm2) {
    resolution.active_pairs = active_pairs;
    resolution.contact_area_mm2 = contact_area_mm2;
    resolution.pressure_reliable = resolution.resolved &&
                                   active_pairs >= resolution.min_active_pairs &&
                                   contact_area_mm2 > 0.0;
    if (!resolution.resolved) return;
    // Load crossed the interface through too few pairs. This is the clause that
    // catches the case the a-priori one cannot see: a geometry whose candidates look
    // fine until the solve reveals that only a couple of them ever touch.
    if (active_pairs < resolution.min_active_pairs && contact_force_n > 0.0) {
        char text[704];
        // The interface facts to quote differ with the criterion: a clearance has a
        // number to compare against, two touching faces do not (printing
        // "clearance 0 mm" told a reader to resolve a gap that does not exist).
        char interface_facts[288];
        if (criterion_is(resolution.criterion, kCriterionPatchArea))
            std::snprintf(interface_facts, sizeof(interface_facts),
                          "%.4g mm interface element size on a %.4g mm^2 candidate "
                          "patch (the faces touch, so there is no clearance to "
                          "resolve: %.0f elements across the patch is the bar, i.e. "
                          "%.3g mm or finer)",
                          resolution.h_interface_mm, resolution.candidate_area_mm2,
                          kContactMinPatchElems, resolution.max_h_interface_mm);
        else
            std::snprintf(interface_facts, sizeof(interface_facts),
                          "%.4g mm interface element size (clearance %.4g mm)",
                          resolution.h_interface_mm, resolution.gap_mm);
        std::snprintf(
            text, sizeof(text),
            "the contact load crossed this interface through %d active pair(s) of %d "
            "candidates, carrying %.4g N: fewer than %d pairs cannot represent a "
            "distributed contact patch, so the reported pressure is a point-load "
            "artefact of the %s. Set contact.interface_size_mm to %.3g (mm) or finer "
            "— it refines the candidate faces LOCALLY, not the whole part",
            active_pairs, resolution.candidate_pairs, contact_force_n,
            resolution.min_active_pairs, interface_facts,
            resolution.required_interface_size_mm);
        resolution.reason = text;
        resolution.resolved = false;
        resolution.pressure_reliable = false;
    }
}

void fea_contact_resolution_load_path(ContactResolution& resolution,
                                      double applied_load_n, double carried_load_n,
                                      bool held_by_contact_only) {
    resolution.applied_load_n = applied_load_n;
    resolution.carried_load_n = carried_load_n;
    resolution.load_fraction = 0.0;
    resolution.load_path_note.clear();
    char text[768];
    if (!held_by_contact_only) {
        // Every body reaches a fixture without the interface. Whatever the pairs
        // picked up is the share of a parallel stiffness path — the number the
        // keyway isolate reported as a seat pressure while both bodies were
        // grounded and the contact carried 0.7 % of the load.
        resolution.contact_carries_load = false;
        resolution.pressure_reliable = false;
        std::snprintf(
            text, sizeof(text),
            "the contact is not the load path: every body in this study is grounded "
            "independently of the interface, so it is a REDUNDANT parallel path and "
            "the %.4g N it picked up is whatever share its stiffness took, not the "
            "load. Quote that resultant as such and do not read force/area as a seat "
            "pressure. To measure the seat, fixture only the bodies that are meant to "
            "be held and let the contact carry the rest — check that the fixture face "
            "set does not reach across the interface onto the mating body",
            carried_load_n);
        resolution.load_path_note = text;
        return;
    }
    if (!(applied_load_n > kLoadPathMinAppliedN)) {
        // A body IS held only by contact, but nothing external pulls on it: there is
        // no load to trace across the interface, so there is no fraction to judge.
        // Not a refusal, and not a proof either.
        resolution.contact_carries_load = carried_load_n > 0.0;
        std::snprintf(text, sizeof(text),
                      "no external load is applied to the body(ies) held only by "
                      "contact, so the %.4g N across the interface is not a traced "
                      "load path — load_fraction is not defined for this study",
                      carried_load_n);
        resolution.load_path_note = text;
        return;
    }
    resolution.load_fraction = carried_load_n / applied_load_n;
    resolution.contact_carries_load = resolution.load_fraction >= kMinLoadFraction;
    if (resolution.contact_carries_load) return;
    std::snprintf(
        text, sizeof(text),
        "the contact did not carry the load: %.4g N of the %.4g N applied to the "
        "body(ies) held only by this interface crossed it (%.3g %%, bar %.0f %%). A "
        "converged solve that routes the load somewhere else answers a different "
        "question than the one asked, and its force/area is not the seat pressure. "
        "The usual cause is a fixture that also grounds the mating body — a face "
        "set or filter that reached across the interface — which grounds both sides "
        "and leaves the contact carrying nothing; the other is an interface that "
        "separated under this load direction",
        carried_load_n, applied_load_n, 100.0 * resolution.load_fraction,
        100.0 * kMinLoadFraction);
    resolution.load_path_note = text;
    resolution.pressure_reliable = false;
    if (resolution.resolved) {
        resolution.reason = resolution.load_path_note;
        resolution.resolved = false;
    }
}

}  // namespace chudware::sim
