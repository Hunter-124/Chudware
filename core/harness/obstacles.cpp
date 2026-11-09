// Document meshes and explicit keepouts for harness routing and path checks.
#include "harness/obstacles.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <string>
#include <utility>
#include <vector>

namespace chudware::harness {
namespace {
using json = nlohmann::json;
constexpr size_t kMaxOwners = 4096;
constexpr size_t kMaxTriangles = 1000000;
constexpr size_t kMaxName = 256;
// BVH centroids and extents are computed as floats, including sums of three vertices.
constexpr double kMaxCoordinate = 1e30;

struct Selector {
    bool by_id = false;
    int id = 0;
    std::string text;
};

bool selector(const json& value, Selector& out) {
    if (value.is_string()) {
        const auto& text = value.get_ref<const std::string&>();
        if (text.empty() || text.size() > kMaxName) return false;
        out.text = text;
        return true;
    }
    if (value.is_number_unsigned()) {
        const auto n = value.get<json::number_unsigned_t>();
        if (n > static_cast<json::number_unsigned_t>(std::numeric_limits<int>::max())) return false;
        out.by_id = true;
        out.id = static_cast<int>(n);
        return true;
    }
    if (value.is_number_integer()) {
        const auto n = value.get<json::number_integer_t>();
        if (n < 0 || n > std::numeric_limits<int>::max()) return false;
        out.by_id = true;
        out.id = static_cast<int>(n);
        return true;
    }
    return false;
}

bool matches(const Selector& s, const SceneBody& b) {
    if (s.by_id) return b.id == s.id;
    return b.name == s.text || (!b.component.empty() && b.component == s.text) ||
           std::to_string(b.id) == s.text;
}

std::string selector_text(const Selector& s) {
    return s.by_id ? std::to_string(s.id) : s.text;
}

bool number(const json& value, double& out) {
    if (!value.is_number()) return false;
    out = value.get<double>();
    return std::isfinite(out);
}

bool point(const json& value, V3& p) {
    if (!value.is_array() || value.size() != 3) return false;
    for (int i = 0; i < 3; ++i) {
        if (!number(value[static_cast<size_t>(i)], p[i]) ||
            std::abs(p[i]) > kMaxCoordinate) return false;
    }
    return true;
}

void add_box(std::vector<TriF>& triangles, const V3& lo, const V3& hi, int owner) {
    const V3 v[8] = {{lo.x, lo.y, lo.z}, {hi.x, lo.y, lo.z},
                     {hi.x, hi.y, lo.z}, {lo.x, hi.y, lo.z},
                     {lo.x, lo.y, hi.z}, {hi.x, lo.y, hi.z},
                     {hi.x, hi.y, hi.z}, {lo.x, hi.y, hi.z}};
    static constexpr int faces[12][3] = {{0, 2, 1}, {0, 3, 2}, {4, 5, 6}, {4, 6, 7},
                                          {0, 1, 5}, {0, 5, 4}, {1, 2, 6}, {1, 6, 5},
                                          {2, 3, 7}, {2, 7, 6}, {3, 0, 4}, {3, 4, 7}};
    for (const auto& face : faces) {
        TriF t{};
        t.owner = owner;
        for (int j = 0; j < 3; ++j)
            for (int k = 0; k < 3; ++k) t.v[3 * j + k] = static_cast<float>(v[face[j]][k]);
        triangles.push_back(t);
    }
}
}  // namespace

std::string ObstacleSet::label(int owner) const {
    if (owner < 0 || static_cast<size_t>(owner) >= owners.size()) return "unknown obstacle";
    const auto& o = owners[static_cast<size_t>(owner)];
    if (o.keepout) return "keepout:" + o.name;
    return o.name + " (#" + std::to_string(o.body_id) + ")";
}

std::string build_obstacles(const json& params, const Scene& scene, double od_mm,
                            double default_clearance_mm, ObstacleSet& out) {
    out = ObstacleSet{};
    if (!params.is_object()) return "params must be an object";
    if (!std::isfinite(od_mm) || od_mm < 0 || od_mm > kMaxCoordinate)
        return "od_mm must be finite and non-negative";
    if (!std::isfinite(default_clearance_mm) || default_clearance_mm < 0 ||
        default_clearance_mm > kMaxCoordinate)
        return "clearance_mm must be finite and non-negative";
    if (scene.bodies.size() > kMaxOwners) return "too many scene bodies (maximum 4096)";

    ObstacleSet result;
    std::vector<bool> ignored(scene.bodies.size(), false);
    std::vector<bool> overridden(scene.bodies.size(), false);
    std::vector<double> clearance(scene.bodies.size(), default_clearance_mm);
    if (params.contains("ignore_bodies")) {
        const auto& list = params["ignore_bodies"];
        if (!list.is_array() || list.size() > kMaxOwners)
            return "ignore_bodies must be an array of at most 4096 body ids, names, or components";
        for (size_t i = 0; i < list.size(); ++i) {
            Selector s;
            if (!selector(list[i], s))
                return "ignore_bodies[" + std::to_string(i) + "] must be a non-negative body id or non-empty name/component";
            bool found = false;
            for (size_t j = 0; j < scene.bodies.size(); ++j) {
                if (matches(s, scene.bodies[j])) { ignored[j] = true; found = true; }
            }
            if (!found) return "ignore_bodies[" + std::to_string(i) + "] matches no body: " + selector_text(s);
            result.ignored.push_back(selector_text(s));
        }
    }

    struct Box { V3 lo, hi; std::string name; double clearance; };
    std::vector<Box> boxes;
    if (params.contains("keepouts")) {
        const auto& list = params["keepouts"];
        if (!list.is_array() || list.size() > kMaxOwners)
            return "keepouts must be an array of at most 4096 entries";
        for (size_t i = 0; i < list.size(); ++i) {
            const auto& item = list[i];
            const std::string prefix = "keepouts[" + std::to_string(i) + "]";
            if (!item.is_object()) return prefix + " must be an object";
            // Both refusals spell out the accepted forms: callers otherwise guess field names.
            const char* const forms =
                "; a keepout is {\"aabb\": [[xmin,ymin,zmin],[xmax,ymax,zmax]], \"name\"?, \"clearance_mm\"?} "
                "or {\"body\": id|name|component, \"clearance_mm\"?}";
            for (auto it = item.begin(); it != item.end(); ++it)
                if (it.key() != "body" && it.key() != "aabb" && it.key() != "clearance_mm" && it.key() != "name")
                    return prefix + " has unknown field: " + it.key() + forms;
            const bool body = item.contains("body"), box = item.contains("aabb");
            if (body == box) return prefix + " must specify exactly one of body or aabb" + forms;
            double gap = default_clearance_mm;
            if (item.contains("clearance_mm") &&
                (!number(item["clearance_mm"], gap) || gap < 0 || gap > kMaxCoordinate))
                return prefix + ".clearance_mm must be finite and non-negative";
            if (body) {
                if (item.contains("name")) return prefix + ".name is only valid for AABB keepouts";
                Selector s;
                if (!selector(item["body"], s)) return prefix + ".body must be a body id, name, or component";
                bool found = false;
                for (size_t j = 0; j < scene.bodies.size(); ++j) {
                    if (!matches(s, scene.bodies[j])) continue;
                    found = true;
                    if (ignored[j]) return prefix + ".body references an ignored body: " + selector_text(s);
                    if (overridden[j]) return prefix + ".body duplicates a previous body keepout: " + selector_text(s);
                    overridden[j] = true;
                    clearance[j] = gap;
                }
                if (!found) return prefix + ".body matches no body: " + selector_text(s);
            } else {
                const auto& bounds = item["aabb"];
                V3 lo, hi;
                if (!bounds.is_array() || bounds.size() != 2 || !point(bounds[0], lo) || !point(bounds[1], hi))
                    return prefix + ".aabb must be [[x,y,z],[x,y,z]] with finite coordinates";
                for (int k = 0; k < 3; ++k)
                    if (!(static_cast<float>(lo[k]) < static_cast<float>(hi[k])))
                        return prefix + ".aabb must have strictly increasing bounds representable in the scene";
                std::string name = std::to_string(i + 1);
                if (item.contains("name")) {
                    if (!item["name"].is_string()) return prefix + ".name must be a non-empty string";
                    const auto& supplied_name = item["name"].get_ref<const std::string&>();
                    if (supplied_name.empty() || supplied_name.size() > kMaxName)
                        return prefix + ".name must be a non-empty string of at most 256 bytes";
                    name = supplied_name;
                }
                boxes.push_back({lo, hi, std::move(name), gap});
            }
        }
    }

    std::vector<TriF> triangles;
    auto add_owner = [&](int id, const std::string& name, bool keepout, double gap) {
        result.owners.push_back({id, name, keepout, gap});
        result.thr.push_back(gap + od_mm / 2.0);
    };
    for (size_t j = 0; j < scene.bodies.size(); ++j) {
        if (ignored[j]) continue;
        const auto& b = scene.bodies[j];
        const std::string prefix =
            (b.name.empty() || b.name.size() > kMaxName ? std::string("body") : b.name) +
            " (#" + std::to_string(b.id) + ")";
        if (b.pos.size() / 3 > 3 * kMaxTriangles)
            return prefix + " has too many vertices (maximum 3000000)";
        if (b.name.size() > kMaxName || b.component.size() > kMaxName)
            return prefix + " has a name or component longer than 256 bytes";
        if (b.pos.empty() || b.pos.size() % 3 != 0 || b.idx.empty() || b.idx.size() % 3 != 0)
            return prefix + " has missing or malformed triangle geometry";
        if (b.idx.size() / 3 > kMaxTriangles - triangles.size())
            return "obstacle geometry exceeds 1000000 triangles";
        for (float x : b.pos)
            if (!std::isfinite(x) || std::abs(static_cast<double>(x)) > kMaxCoordinate)
                return prefix + " has non-finite or out-of-range vertex coordinates";
        if (result.owners.size() >= kMaxOwners) return "too many obstacle owners (maximum 4096)";
        const int owner = static_cast<int>(result.owners.size());
        const size_t before = triangles.size();
        size_t degenerate = 0;
        for (size_t n = 0; n < b.idx.size(); n += 3) {
            TriF t{};
            t.owner = owner;
            for (size_t v = 0; v < 3; ++v) {
                const size_t vertex = b.idx[n + v];
                if (vertex >= b.pos.size() / 3) return prefix + " has an out-of-range triangle index";
                for (size_t k = 0; k < 3; ++k) t.v[3 * v + k] = b.pos[3 * vertex + k];
            }
            const V3 a(t.v[0], t.v[1], t.v[2]), c(t.v[3], t.v[4], t.v[5]), d(t.v[6], t.v[7], t.v[8]);
            if (norm2(cross(c - a, d - a)) == 0.0) { ++degenerate; continue; }
            triangles.push_back(t);
        }
        const bool usable = triangles.size() > before;
        if (degenerate > 0) {
            std::string note = prefix + ": skipped " + std::to_string(degenerate) +
                               " degenerate (zero-area) display triangles";
            if (!usable) note += "; no usable triangles remain, body ignored";
            result.warnings.push_back(std::move(note));
        }
        if (usable) add_owner(b.id, b.name.empty() ? "body" : b.name, false, clearance[j]);
    }
    for (const auto& box : boxes) {
        if (result.owners.size() >= kMaxOwners) return "too many obstacle owners (maximum 4096)";
        if (triangles.size() > kMaxTriangles - 12) return "obstacle geometry exceeds 1000000 triangles";
        const int owner = static_cast<int>(result.owners.size());
        add_owner(0, box.name, true, box.clearance);
        add_box(triangles, box.lo, box.hi, owner);
    }
    result.triangle_count = triangles.size();
    if (!result.thr.empty()) {
        const auto range = std::minmax_element(result.thr.begin(), result.thr.end());
        result.thr_min = *range.first;
        result.thr_max = *range.second;
    }
    result.has_geometry = !triangles.empty();
    if (!result.has_geometry) result.warnings.push_back("No obstacle geometry is present; clearance is unchecked.");
    result.bvh.build(std::move(triangles));
    if (result.has_geometry) { result.lo = result.bvh.lo(); result.hi = result.bvh.hi(); }
    out = std::move(result);
    return {};
}
}  // namespace chudware::harness
