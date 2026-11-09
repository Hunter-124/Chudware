#include "sketch_model_snap.h"

#include <algorithm>
#include <cctype>
#include <unordered_set>
#include <cmath>
#include <cstring>
#include <map>

namespace ui {

namespace {

constexpr float kMinProjectedMm = 0.01f;   // an edge projecting shorter than this is edge-on: no curve
constexpr float kMinCircleCos = 0.25f;     // |cos| of circle-plane vs sketch-plane normals; edge-on circles collapse
constexpr double kFitTol = 1e-3;           // relative radial / planar tolerance of the circle fit
constexpr size_t kMaxKeyTests = 6;         // nearest key points confirmed against the id buffer per query
constexpr size_t kMaxEdgeTests = 4;        // nearest edges confirmed per query

using Vec = std::array<double, 3>;
Vec sub(const Vec& a, const Vec& b) { return {a[0] - b[0], a[1] - b[1], a[2] - b[2]}; }
double dot(const Vec& a, const Vec& b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
Vec cross(const Vec& a, const Vec& b) {
    return {a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]};
}
double norm(const Vec& a) { return std::sqrt(dot(a, a)); }
Vec at(const float* p, size_t i) { return {p[3 * i], p[3 * i + 1], p[3 * i + 2]}; }

// Circle through three non-collinear points: center, unit plane normal, radius.
bool circumcircle(const Vec& a, const Vec& b, const Vec& c, Vec& center, Vec& normal,
                  double& radius) {
    const Vec p = sub(b, a), q = sub(c, a);
    const Vec axb = cross(p, q);
    const double n2 = dot(axb, axb);
    if (n2 < 1e-12) return false;
    const double pp = dot(p, p), qq = dot(q, q);
    const Vec w = {pp * q[0] - qq * p[0], pp * q[1] - qq * p[1], pp * q[2] - qq * p[2]};
    const Vec t = cross(w, axb);
    center = {a[0] + t[0] / (2.0 * n2), a[1] + t[1] / (2.0 * n2), a[2] + t[2] / (2.0 * n2)};
    radius = norm(sub(center, a));
    const double len = std::sqrt(n2);
    normal = {axb[0] / len, axb[1] / len, axb[2] / len};
    return radius > 1e-6;
}

template <class T, size_t N, class Less>
void keepBest(std::array<T, N>& best, size_t& count, const T& c, Less less) {
    size_t i;
    if (count < N) i = count++;
    else if (less(c, best[N - 1])) i = N - 1;
    else return;
    best[i] = c;
    for (; i > 0 && less(best[i], best[i - 1]); --i) std::swap(best[i], best[i - 1]);
}

}  // namespace

void SketchModelSnap::clear() {
    mesh_.reset();
    valid_ = false;
    std::vector<ModelSnapHit>().swap(keys_);
    std::vector<Edge>().swap(edges_);
    std::vector<float>().swap(uv_);
    std::vector<float>().swap(xyz_);
}

bool SketchModelSnap::upToDate(const std::shared_ptr<const render::DocMesh>& mesh,
                               const float frame[16], uint64_t filterKey) const {
    return valid_ && mesh && !mesh_.owner_before(mesh) && !mesh.owner_before(mesh_) &&
           filterKey_ == filterKey && std::memcmp(frame_, frame, sizeof(frame_)) == 0;
}

void SketchModelSnap::sync(const std::shared_ptr<const render::DocMesh>& mesh,
                           const float frame[16], uint64_t filterKey, const EdgeFilter& excluded) {
    if (!mesh) { clear(); return; }
    if (upToDate(mesh, frame, filterKey)) return;
    mesh_ = mesh;
    std::memcpy(frame_, frame, sizeof(frame_));
    filterKey_ = filterKey;
    build(*mesh, frame, excluded);
    valid_ = true;
    ++builds_;
}

void SketchModelSnap::build(const render::DocMesh& mesh, const float frame[16],
                            const EdgeFilter& excluded) {
    keys_.clear(); edges_.clear(); uv_.clear(); xyz_.clear();
    const Vec O{frame[3], frame[7], frame[11]}, U{frame[0], frame[4], frame[8]},
              V{frame[1], frame[5], frame[9]}, N{frame[2], frame[6], frame[10]};
    auto project = [&](const Vec& p, float& u, float& v) {
        const Vec d = sub(p, O);
        u = float(dot(d, U));
        v = float(dot(d, V));
    };
    // A vertex has no owner of its own: it stays referenceable while an edge that is not excluded
    // still ends on it. Positions are compared on a 0.01 mm grid, probing the neighbouring cells.
    std::unordered_set<uint64_t> anchors;
    auto cell = [](float x) { return int64_t(std::llround(double(x) * 100.0)); };
    auto cellKey = [](int64_t x, int64_t y, int64_t z) {
        return uint64_t(x) * 73856093u ^ uint64_t(y) * 19349663u ^ uint64_t(z) * 83492791u;
    };
    if (excluded)
        for (const auto& b : mesh.bodies) {
            const size_t total = b.edge_pts.size() / 3;
            for (const auto& es : b.edges) {
                if (es.pt_count < 2 || size_t(es.first_pt) + es.pt_count > total ||
                    excluded(b.body_id, es.edge_id))
                    continue;
                for (size_t end : {size_t(0), size_t(es.pt_count) - 1}) {
                    const float* q = &b.edge_pts[(size_t(es.first_pt) + end) * 3];
                    anchors.insert(cellKey(cell(q[0]), cell(q[1]), cell(q[2])));
                }
            }
        }
    auto anchored = [&](const float* p) {
        const int64_t x = cell(p[0]), y = cell(p[1]), z = cell(p[2]);
        for (int64_t dx = -1; dx <= 1; ++dx)
            for (int64_t dy = -1; dy <= 1; ++dy)
                for (int64_t dz = -1; dz <= 1; ++dz)
                    if (anchors.count(cellKey(x + dx, y + dy, z + dz))) return true;
        return false;
    };
    for (const auto& b : mesh.bodies) {
        for (const auto& bv : b.brep_verts) {
            if (excluded && !anchored(bv.xyz)) continue;
            ModelSnapHit k;
            k.kind = ModelSnapKind::Vertex; k.body = b.body_id; k.topo = bv.vertex_id;
            project({bv.xyz[0], bv.xyz[1], bv.xyz[2]}, k.u, k.v);
            k.probe[0] = {bv.xyz[0], bv.xyz[1], bv.xyz[2]};
            k.probes = 1;
            keys_.push_back(k);
        }
        const size_t total = b.edge_pts.size() / 3;
        for (const auto& es : b.edges) {
            if (excluded && excluded(b.body_id, es.edge_id)) continue;
            // Only lines and circles project to ONE analytic sketch entity (a constraint can name
            // it); splines come back as several sampled pieces, so they are not snap targets.
            if (es.curve_type > 1 || es.pt_count < 2 || size_t(es.first_pt) + es.pt_count > total)
                continue;
            const float* p = &b.edge_pts[size_t(es.first_pt) * 3];
            const size_t n = es.pt_count;
            const Vec first = at(p, 0), last = at(p, n - 1);

            ModelSnapHit key;
            key.body = b.body_id; key.topo = es.edge_id;
            bool snappable = false;
            if (es.curve_type == 0) {
                snappable = true;
                key.kind = ModelSnapKind::Midpoint;
                const Vec mid = {(first[0] + last[0]) * 0.5, (first[1] + last[1]) * 0.5,
                                 (first[2] + last[2]) * 0.5};
                project(mid, key.u, key.v);
                key.probe[0] = {float(mid[0]), float(mid[1]), float(mid[2])};
                key.probes = 1;
            } else {
                const bool closed = norm(sub(first, last)) < 1e-3;
                const size_t i1 = closed ? n / 3 : n / 2, i2 = closed ? (2 * n) / 3 : n - 1;
                Vec center, normal;
                double radius = 0;
                if (i1 > 0 && i2 > i1 && i2 < n &&
                    circumcircle(first, at(p, i1), at(p, i2), center, normal, radius) &&
                    std::fabs(dot(normal, N)) >= kMinCircleCos) {
                    const double tol = kFitTol * radius + 1e-4;
                    snappable = true;
                    for (size_t i = 0; i < n && snappable; ++i) {
                        const Vec d = sub(at(p, i), center);
                        snappable = std::fabs(norm(d) - radius) <= tol &&
                                    std::fabs(dot(d, normal)) <= tol;
                    }
                    if (snappable) {
                        key.kind = ModelSnapKind::Center;
                        project(center, key.u, key.v);
                        const size_t samples[4] = {0, n / 4, n / 2, (3 * n) / 4};
                        for (size_t s : samples) {
                            key.probe[size_t(key.probes++)] = {p[3 * s], p[3 * s + 1], p[3 * s + 2]};
                        }
                    }
                }
            }
            if (!snappable) continue;

            Edge e;
            e.body = b.body_id; e.topo = es.edge_id;
            e.first = uv_.size() / 2; e.count = n;
            float length = 0;
            for (size_t i = 0; i < n; ++i) {
                float u, v;
                project(at(p, i), u, v);
                if (i) length += std::hypot(u - uv_[uv_.size() - 2], v - uv_.back());
                uv_.push_back(u); uv_.push_back(v);
                xyz_.push_back(p[3 * i]); xyz_.push_back(p[3 * i + 1]); xyz_.push_back(p[3 * i + 2]);
            }
            if (length < kMinProjectedMm) {   // edge-on: it projects to a point, not a curve
                uv_.resize(e.first * 2); xyz_.resize(e.first * 3);
                continue;
            }
            e.umin = e.umax = uv_[e.first * 2]; e.vmin = e.vmax = uv_[e.first * 2 + 1];
            for (size_t i = 0; i < n; ++i) {
                const float u = uv_[(e.first + i) * 2], v = uv_[(e.first + i) * 2 + 1];
                e.umin = std::min(e.umin, u); e.umax = std::max(e.umax, u);
                e.vmin = std::min(e.vmin, v); e.vmax = std::max(e.vmax, v);
            }
            edges_.push_back(e);
            keys_.push_back(key);
        }
    }
}

bool SketchModelSnap::query(const ModelSnapScreen& s, float cx, float cy, float reach,
                            bool inference, bool onEdge, const Visible& visible,
                            ModelSnapHit& out) const {
    const float r2 = reach * reach;

    // Key points: vertices first, then midpoints / centers; nearest first inside each group.
    struct KeyCand { float d2; int rank; size_t index; };
    std::array<KeyCand, kMaxKeyTests> keyBest;
    size_t keyCount = 0;
    auto keyLess = [](const KeyCand& a, const KeyCand& b) {
        return a.rank != b.rank ? a.rank < b.rank : a.d2 < b.d2;
    };
    for (size_t i = 0; i < keys_.size(); ++i) {
        const ModelSnapHit& k = keys_[i];
        const int rank = k.kind == ModelSnapKind::Vertex ? 0 : 1;
        if (rank && !inference) continue;
        const float dx = s.x(k.u, k.v) - cx, dy = s.y(k.u, k.v) - cy;
        const float d2 = dx * dx + dy * dy;
        if (d2 <= r2) keepBest(keyBest, keyCount, KeyCand{d2, rank, i}, keyLess);
    }
    for (size_t i = 0; i < keyCount; ++i)
        if (visible(keys_[keyBest[i].index])) { out = keys_[keyBest[i].index]; return true; }
    if (!inference || !onEdge) return false;

    // On-edge: the nearest point of any straight / circular edge's projected polyline. Edges are
    // culled by their sketch-space bounding box grown by the reach, mapped back through the basis.
    const float det = s.ux * s.vy - s.uy * s.vx;
    if (std::fabs(det) < 1e-9f) return false;
    const float dx0 = cx - s.ox, dy0 = cy - s.oy;
    const float cu = (s.vy * dx0 - s.vx * dy0) / det, cv = (-s.uy * dx0 + s.ux * dy0) / det;
    const float ru = reach * std::hypot(s.vx, s.vy) / std::fabs(det);
    const float rv = reach * std::hypot(s.ux, s.uy) / std::fabs(det);

    struct EdgeCand { float d2; size_t edge, seg; float t; };
    std::array<EdgeCand, kMaxEdgeTests> edgeBest;
    size_t edgeCount = 0;
    auto edgeLess = [](const EdgeCand& a, const EdgeCand& b) { return a.d2 < b.d2; };
    for (size_t ei = 0; ei < edges_.size(); ++ei) {
        const Edge& e = edges_[ei];
        if (cu < e.umin - ru || cu > e.umax + ru || cv < e.vmin - rv || cv > e.vmax + rv) continue;
        EdgeCand nearest{r2 + 1.0f, ei, 0, 0.0f};
        for (size_t i = 0; i + 1 < e.count; ++i) {
            const float* a = &uv_[(e.first + i) * 2];
            const float ax = s.x(a[0], a[1]), ay = s.y(a[0], a[1]);
            const float bx = s.x(a[2], a[3]), by = s.y(a[2], a[3]);
            const float vx = bx - ax, vy = by - ay;
            const float len2 = vx * vx + vy * vy;
            float t = len2 > 1e-12f ? ((cx - ax) * vx + (cy - ay) * vy) / len2 : 0.0f;
            t = std::max(0.0f, std::min(1.0f, t));
            const float px = ax + t * vx - cx, py = ay + t * vy - cy;
            const float d2 = px * px + py * py;
            if (d2 < nearest.d2) nearest = EdgeCand{d2, ei, i, t};
        }
        if (nearest.d2 <= r2) keepBest(edgeBest, edgeCount, nearest, edgeLess);
    }
    for (size_t i = 0; i < edgeCount; ++i) {
        const EdgeCand& c = edgeBest[i];
        const Edge& e = edges_[c.edge];
        const float* a = &uv_[(e.first + c.seg) * 2];
        const float* w = &xyz_[(e.first + c.seg) * 3];
        ModelSnapHit hit;
        hit.kind = ModelSnapKind::OnEdge; hit.body = e.body; hit.topo = e.topo;
        hit.u = a[0] + c.t * (a[2] - a[0]);
        hit.v = a[1] + c.t * (a[3] - a[1]);
        for (int k = 0; k < 3; ++k) hit.probe[0][size_t(k)] = w[k] + c.t * (w[3 + k] - w[k]);
        hit.probes = 1;
        if (visible(hit)) { out = hit; return true; }
    }
    return false;
}

// ---------------------------------------------------------------------------------------------
namespace modelref {

namespace {

// Parses a leading "@m<digits>" whose remainder is empty or a ".sub" handle.
bool parseToken(const std::string& s, int& k, size_t& end) {
    if (s.size() < 3 || s[0] != '@' || s[1] != 'm' || !std::isdigit((unsigned char)s[2])) return false;
    size_t i = 2;
    long value = 0;
    while (i < s.size() && std::isdigit((unsigned char)s[i])) {
        value = value * 10 + (s[i] - '0');
        if (value > 1000000) return false;
        ++i;
    }
    if (i != s.size() && s[i] != '.') return false;
    k = int(value); end = i;
    return true;
}

// A batch placeholder: "$N", "$N#k" or "$N.handle" (handle = identifier: p1, center, mid...).
// `end` = first byte after N. Anything looser ("$5.00" in a Text entity) is ordinary text.
bool parsePlaceholder(const std::string& s, long& n, size_t& end) {
    if (s.size() < 2 || s[0] != '$' || !std::isdigit((unsigned char)s[1])) return false;
    size_t i = 1;
    long value = 0;
    while (i < s.size() && std::isdigit((unsigned char)s[i])) {
        value = value * 10 + (s[i] - '0');
        if (value > 1000000) return false;
        ++i;
    }
    const size_t digitsEnd = i;
    if (i < s.size() && s[i] == '#') {
        const size_t k = ++i;
        while (i < s.size() && std::isdigit((unsigned char)s[i])) ++i;
        if (i == k) return false;
    }
    if (i < s.size() && s[i] == '.') {
        ++i;
        if (i >= s.size() || !(std::isalpha((unsigned char)s[i]) || s[i] == '_')) return false;
        while (i < s.size() && (std::isalnum((unsigned char)s[i]) || s[i] == '_')) ++i;
    }
    if (i != s.size()) return false;
    n = value; end = digitsEnd;
    return true;
}

template <class Fn>
void forEachString(nlohmann::json& node, const Fn& fn) {
    if (node.is_string()) { std::string s = node.get<std::string>(); if (fn(s)) node = std::move(s); }
    else if (node.is_array()) for (auto& e : node) forEachString(e, fn);
    else if (node.is_object()) for (auto it = node.begin(); it != node.end(); ++it) forEachString(it.value(), fn);
}

// Invokes fn(refs) for the "refs" list of every constraint in a batch: sketch_add_entity's inline
// `constraints` and sketch_add_constraint's `constraint`. Model-reference tokens live nowhere
// else, so entity payloads (a Text entity's content, say) are never rewritten.
template <class Json, class Fn>
void forEachConstraintRefs(Json& ops, const Fn& fn) {
    if (!ops.is_array()) return;
    for (auto& op : ops) {
        if (!op.is_object()) continue;
        const auto inline_ = op.find("constraints");
        if (inline_ != op.end() && inline_->is_array())
            for (auto& c : *inline_) {
                const auto refs = c.find("refs");
                if (refs != c.end()) fn(*refs);
            }
        const auto one = op.find("constraint");
        if (one != op.end()) {
            const auto refs = one->find("refs");
            if (refs != one->end()) fn(*refs);
        }
    }
}

void collectTokens(const nlohmann::json& ops, std::vector<int>& ks) {
    forEachConstraintRefs(ops, [&](const nlohmann::json& refs) {
        if (!refs.is_array()) return;
        for (const auto& r : refs) {
            int k; size_t end;
            if (r.is_string() && parseToken(r.get_ref<const std::string&>(), k, end) &&
                std::find(ks.begin(), ks.end(), k) == ks.end())
                ks.push_back(k);
        }
    });
}

}  // namespace

std::string snapRecord(ModelSnapKind kind, int k) {
    const std::string tok = k < 0 ? std::string("@m") : "@m" + std::to_string(k);
    switch (kind) {
        case ModelSnapKind::Vertex:   return tok + ".p";
        case ModelSnapKind::Midpoint: return "mid:" + tok;
        case ModelSnapKind::Center:   return tok + ".center";
        case ModelSnapKind::OnEdge:   return "rim:" + tok;
    }
    return {};
}

std::string withRef(const std::string& hoverRecord, int k) {
    const size_t at = hoverRecord.find("@m");
    if (at == std::string::npos) return hoverRecord;
    std::string out = hoverRecord;
    out.insert(at + 2, std::to_string(k));
    return out;
}

bool uses(const nlohmann::json& ops) {
    std::vector<int> ks;
    collectTokens(ops, ks);
    return !ks.empty();
}

std::string convertedEntity(const nlohmann::json& tree, const std::string& sketchFid,
                            const nlohmann::json& ref) {
    if (!tree.is_object() || !tree.contains("features") || !tree["features"].is_array()) return {};
    for (const auto& f : tree["features"]) {
        if (!f.is_object() || f.value("id", std::string()) != sketchFid) continue;
        const auto params = f.find("params");
        if (params == f.end() || !params->is_object()) return {};
        const auto refs = params->find("external_refs");
        if (refs == params->end() || !refs->is_array()) return {};
        for (const auto& xr : *refs) {
            if (!xr.is_object() || xr.value("stale", false) || xr.value("source", nlohmann::json()) != ref)
                continue;
            const auto ids = xr.find("entity_ids");
            if (ids != xr.end() && ids->is_array() && !ids->empty() && (*ids)[0].is_string())
                return (*ids)[0].get<std::string>();
        }
        return {};
    }
    return {};
}

int bind(nlohmann::json& ops, const std::string& sketchFid,
         const std::vector<nlohmann::json>& refs,
         const std::function<std::string(const nlohmann::json&)>& lookup) {
    std::vector<int> ks;
    collectTokens(ops, ks);
    std::sort(ks.begin(), ks.end());
    std::map<int, std::string> target;   // token index -> literal entity id or "$j" placeholder
    std::vector<int> fresh;              // tokens that need a sketch_convert, in op order
    for (int k : ks) {
        if (k < 0 || size_t(k) >= refs.size()) continue;   // unknown token: the engine rejects it by name
        const std::string existing = lookup(refs[size_t(k)]);
        if (!existing.empty()) target[k] = existing;
        else fresh.push_back(k);
    }
    const int shift = int(fresh.size());
    if (shift) {
        // The batch's own placeholders move behind the prepended converts.
        forEachString(ops, [&](std::string& s) {
            long n; size_t end;
            if (!parsePlaceholder(s, n, end)) return false;
            s = "$" + std::to_string(n + shift) + s.substr(end);
            return true;
        });
        for (int j = 0; j < shift; ++j) target[fresh[size_t(j)]] = "$" + std::to_string(j);
    }
    forEachConstraintRefs(ops, [&](nlohmann::json& refs) {
        forEachString(refs, [&](std::string& s) {
            int k; size_t end;
            if (!parseToken(s, k, end)) return false;
            const auto it = target.find(k);
            if (it == target.end()) return false;
            s = it->second + s.substr(end);
            return true;
        });
    });
    if (shift) {
        nlohmann::json converts = nlohmann::json::array();
        for (int k : fresh)
            converts.push_back({{"op", "sketch_convert"}, {"sketch", sketchFid},
                                {"refs", nlohmann::json::array({refs[size_t(k)]})},
                                {"construction", true}, {"fix", true}, {"associative", true}});
        ops.insert(ops.begin(), converts.begin(), converts.end());
    }
    return shift;
}

nlohmann::json restoreIdMap(const nlohmann::json& idMap, int shift) {
    if (!idMap.is_object() || shift <= 0) return idMap;
    nlohmann::json out = nlohmann::json::object();
    for (auto it = idMap.begin(); it != idMap.end(); ++it) {
        long n; size_t end;
        if (!parsePlaceholder(it.key(), n, end)) { out[it.key()] = it.value(); continue; }
        if (n < shift) continue;   // the prepended converts
        out["$" + std::to_string(n - shift) + it.key().substr(end)] = it.value();
    }
    return out;
}

namespace {

// True when any string below `node` names one of `ids`. A feature's bulk sketch payload
// (entities / constraints) never carries feature ids and is skipped.
bool mentions(const nlohmann::json& node, const std::set<std::string>& ids) {
    if (node.is_string()) return ids.count(node.get_ref<const std::string&>()) != 0;
    if (node.is_array()) {
        for (const auto& e : node) if (mentions(e, ids)) return true;
    } else if (node.is_object()) {
        for (auto it = node.begin(); it != node.end(); ++it) {
            if (it.key() == "entities" || it.key() == "constraints") continue;
            if (mentions(it.value(), ids)) return true;
        }
    }
    return false;
}

}  // namespace

std::set<std::string> downstreamOf(const nlohmann::json& tree, const std::string& sketchFid) {
    std::set<std::string> downstream;
    if (!tree.is_object() || !tree.contains("features") || !tree["features"].is_array())
        return downstream;
    std::set<std::string> reach{sketchFid};
    bool after = false;
    for (const auto& f : tree["features"]) {
        if (!f.is_object()) continue;
        const std::string id = f.value("id", std::string());
        if (id == sketchFid) { after = true; continue; }
        if (!after || id.empty() || !mentions(f, reach)) continue;
        reach.insert(id);
        downstream.insert(id);
    }
    return downstream;
}

}  // namespace modelref

}  // namespace ui
