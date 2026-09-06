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

    const double k_pair = E_mpa * out.char_len_mm * options.penalty_scale;
    std::map<std::array<int, 2>, int> group_of;
    std::map<std::array<int, 2>, char> paired;  // (min node, max node) dedupe
    struct Best {
        double score = 0.0;
        int node = -1;
        int dir = -1;
        double gap = 0.0;
    };
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
            const std::array<int, 2> comps{std::min(ci, cj), std::max(ci, cj)};
            auto git = group_of.find(comps);
            if (git == group_of.end()) {
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
            s.k = k_pair;
            s.gap0_mm = b.gap;
            for (int c = 0; c < 3; ++c)
                s.normal[static_cast<std::size_t>(c)] = flip ? -dir.n[c] : dir.n[c];
            s.area_mm2 = dir.area;
            s.group = git->second;
            out.springs.push_back(s);
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
        // "Can seat": within twice the closest approach. A pair whose initial gap is
        // more than that needs the interface to deform by more than the clearance
        // itself before it touches, which is outside small-deflection contact.
        const double seat_limit = out.min_gap0_mm > 0.0 ? 2.0 * out.min_gap0_mm : 0.0;
        for (double gap : gaps)
            if (gap <= seat_limit) ++out.seatable_pairs;
    }
    return out;
}

ContactResolution fea_contact_resolution(const ContactCandidateSet& candidates) {
    ContactResolution out;
    out.min_active_pairs = kContactMinActivePairs;
    out.candidate_pairs = static_cast<int>(candidates.springs.size());
    out.seatable_pairs = candidates.seatable_pairs;
    out.h_interface_mm = candidates.interface_h_mm;
    out.candidate_area_mm2 = candidates.candidate_area_mm2;
    // The clearance the discretization has to resolve is the interface's own closest
    // approach. A set whose closest pair is TOUCHING or interfering (gap <= 0) has no
    // clearance to resolve, so the ratio is undefined and the patch criterion applies
    // instead (fea_contact.h).
    out.gap_mm = candidates.min_gap0_mm > 0.0 ? candidates.min_gap0_mm : 0.0;
    const bool touching = !(out.gap_mm > 0.0);
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
    // The size that resolves this interface: kContactMaxHOverGap elements' worth of
    // the clearance (or kContactMinPatchElems across the patch when the faces touch),
    // and never coarser than half the size that failed — an interface that funnelled
    // at a legal ratio still has to be refined to place more pairs.
    const double from_geometry = touching ? out.max_h_interface_mm
                                          : kContactMaxHOverGap * out.gap_mm;
    out.required_interface_size_mm =
        out.h_interface_mm > 0.0 && from_geometry > 0.0
            ? std::min(from_geometry, out.h_interface_mm * 0.5)
            : (from_geometry > 0.0 ? from_geometry : out.h_interface_mm * 0.5);
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
                "pairs are in contact. So any load would funnel through those few "
                "nodes and be reported as a contact pressure. Set "
                "contact.interface_size_mm to %.3g (mm) or finer: it refines the "
                "candidate faces LOCALLY, so the interface is resolved without "
                "meshing the whole part that fine",
                out.h_interface_mm, out.candidate_area_mm2,
                std::sqrt(out.candidate_area_mm2), kContactMinPatchElems,
                out.seatable_pairs, out.candidate_pairs,
                out.required_interface_size_mm);
            out.reason = text;
            out.resolved = false;
        }
        return out;
    }
    if (out.ratio > kContactMaxHOverGap && out.seatable_pairs < kContactMinActivePairs) {
        char text[640];
        std::snprintf(
            text, sizeof(text),
            "the contact interface is not resolved by this mesh: the element size AT "
            "the interface is %.4g mm against a %.4g mm clearance (ratio %.1f, limit "
            "%.0f), and only %d of %d candidate pairs sit within twice the closest "
            "approach — so any load would funnel through those few nodes and be "
            "reported as a contact pressure. Set contact.interface_size_mm to %.3g "
            "(mm) or finer: it refines the candidate faces LOCALLY, so the interface "
            "is resolved without meshing the whole part that fine",
            out.h_interface_mm, out.gap_mm, out.ratio, kContactMaxHOverGap,
            out.seatable_pairs, out.candidate_pairs, out.required_interface_size_mm);
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
        if (resolution.criterion == kCriterionPatchArea)
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

}  // namespace chudware::sim
