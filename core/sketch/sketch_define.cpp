#include "sketch_define.h"
#include "sketch.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace chudware::sketch {
namespace {
using json = nlohmann::json;
constexpr double pi = 3.14159265358979323846;
constexpr double radians = pi / 180.0;

struct Point {
    double x = 0, y = 0;
    Point operator-(Point p) const { return {x - p.x, y - p.y}; }
    double norm() const { return std::hypot(x, y); }
};
double cross(Point a, Point b) { return a.x * b.y - a.y * b.x; }
double dot(Point a, Point b) { return a.x * b.x + a.y * b.y; }
Point point(const json& p) { return {p.at(0).get<double>(), p.at(1).get<double>()}; }
std::string entity_of(const std::string& ref) { return ref.substr(0, ref.find('.')); }
bool round_curve(const json& e) {
    const auto type = e.value("type", std::string());
    return type == "arc" || type == "circle";
}
bool line_curve(const json& e) { return e.value("type", std::string()) == "line"; }

Point handle_point(const std::string& ref, const json& coords) {
    if (ref == "origin") return {};
    const auto& e = coords.at(entity_of(ref));
    const auto sub = ref.find('.') == std::string::npos ? "" : ref.substr(ref.find('.') + 1);
    const auto type = e.value("type", std::string());
    if (type == "spline" || type == "style_spline") {
        if (sub == "start") return point(e.at("points").front());
        if (sub == "end") return point(e.at("points").back());
        if (!sub.empty() && sub[0] == 'p')
            return point(e.at("points").at(std::stoul(sub.substr(1))));
    }
    if (sub == "center") return point(e.at("center"));
    if (sub == "p1" || sub == "p2") return point(e.at(sub));
    if (type == "arc" && (sub == "start" || sub == "end")) {
        Point c = point(e.at("center"));
        double angle = e.at(sub).get<double>() * radians, r = e.at("radius");
        return {c.x + r * std::cos(angle), c.y + r * std::sin(angle)};
    }
    return point(e.at("p"));
}

std::string canonical_point_handle(const std::string& ref, const json& coords) {
    const auto dot = ref.find('.');
    if (dot == std::string::npos) return ref;
    const auto id = ref.substr(0, dot), sub = ref.substr(dot + 1);
    const auto& e = coords.at(id);
    const auto type = e.value("type", std::string());
    if (type == "spline" || type == "style_spline") {
        if (sub == "start") return id + ".p0";
        if (sub == "end") return id + ".p" + std::to_string(e.at("points").size() - 1);
    }
    return ref;
}

// Constraint components exclude the fixed virtual datums: two circles dimensioned
// to origin are still independent systems. Keep declaration order in each component.
struct Groups {
    std::map<std::string, std::string> parents;
    std::string root(const std::string& id) {
        auto [it, inserted] = parents.emplace(id, id);
        if (inserted || it->second == id) return id;
        return it->second = root(it->second);
    }
    bool join(const std::string& a, const std::string& b) {
        auto x = root(a), y = root(b);
        if (x == y) return false;
        parents[y] = x;
        return true;
    }
};

std::vector<json> components(const json& sketch) {
    Groups groups;
    std::set<std::string> known;
    for (const auto& e : sketch.at("entities")) known.insert(e.at("id").get<std::string>());
    for (const auto& c : sketch.at("constraints")) {
        std::string first;
        for (const auto& ref : c.value("refs", json::array())) {
            auto id = entity_of(ref.get<std::string>());
            if (!known.count(id)) continue;
            if (first.empty()) first = id;
            else groups.join(first, id);
        }
    }
    std::vector<json> result;
    std::map<std::string, std::size_t> indices;
    for (const auto& e : sketch.at("entities")) {
        auto root = groups.root(e.at("id").get<std::string>());
        auto [it, inserted] = indices.emplace(root, result.size());
        if (inserted) result.push_back({{"entities", json::array()}, {"constraints", json::array()}});
        result[it->second]["entities"].push_back(e);
    }
    for (const auto& c : sketch.at("constraints")) {
        for (const auto& ref : c.value("refs", json::array())) {
            auto id = entity_of(ref.get<std::string>());
            if (!known.count(id)) continue;
            result[indices.at(groups.root(id))]["constraints"].push_back(c);
            break;
        }
    }
    return result;
}

bool numeric_match(const json& a, const json& b, double tolerance) {
    if (a.is_number() && b.is_number())
        return std::isfinite(b.get<double>()) &&
               std::abs(a.get<double>() - b.get<double>()) <= tolerance;
    if (a.type() != b.type()) return false;
    if (a.is_object()) {
        for (auto it = a.begin(); it != a.end(); ++it)
            if (!b.contains(it.key()) || !numeric_match(it.value(), b.at(it.key()), tolerance))
                return false;
        return true;
    }
    if (a.is_array()) {
        if (a.size() != b.size()) return false;
        for (std::size_t i = 0; i < a.size(); ++i)
            if (!numeric_match(a[i], b[i], tolerance)) return false;
        return true;
    }
    return a == b;
}

bool geometry_matches(const json& before, const json& after, double tolerance) {
    // A conservative scalar bound also protects spline control points and conics.
    if (!numeric_match(before, after, tolerance * 0.25)) return false;
    for (const auto& ep : entity_endpoints(before))
        if ((handle_point(ep.handle, after) - Point{ep.x, ep.y}).norm() > tolerance * 0.5)
            return false;
    for (auto it = before.begin(); it != before.end(); ++it) {
        const auto& a = it.value();
        const auto& b = after.at(it.key());
        double scale = 1;
        if (a.contains("radius")) scale = std::max(scale, std::abs(a["radius"].get<double>()));
        if (a.contains("rx")) scale = std::max(scale, std::abs(a["rx"].get<double>()));
        if (a.contains("ry")) scale = std::max(scale, std::abs(a["ry"].get<double>()));
        for (const char* key : {"start", "end", "rot"})
            if (a.contains(key) && a[key].is_number() &&
                std::abs(a[key].get<double>() - b[key].get<double>()) * radians * scale > tolerance * 0.25)
                return false;
    }
    return true;
}

std::string signature(const json& c) {
    auto refs = c.value("refs", json::array());
    const auto type = c.value("type", std::string());
    if (type == "coincident" || type == "parallel" || type == "perpendicular" ||
        type == "collinear" || type == "equal" || type == "concentric" || type == "tangent")
        std::sort(refs.begin(), refs.end());
    return type + ":" + refs.dump() + ":" + c.value("axis", std::string());
}

class Definer {
    const json& env_;
    const DefineOptions& opt_;
    json sketch_;
    SketchSolution solved_;
    json original_;
    DefineResult result_;
    std::vector<std::string> ids_;
    std::set<std::string> selected_, considered_, constraint_ids_;
    std::size_t next_id_ = 0;

    bool selected(const std::string& ref) const {
        return selected_.empty() || selected_.count(entity_of(ref));
    }
    bool eligible(const std::string& a, const std::string& b) const {
        return selected(a) || selected(b);
    }
    std::vector<Endpoint> endpoints() const {
        auto result = entity_endpoints(solved_.entities);
        // Construction geometry is still part of Fully Define; the public
        // endpoint helper deliberately omits it for auto-coincidence callers.
        for (const auto& id : ids_) {
            const auto& e = solved_.entities.at(id);
            if (!e.value("construction", false)) continue;
            json visible = e;
            visible["construction"] = false;
            auto extra = entity_endpoints(json{{id, std::move(visible)}});
            result.insert(result.end(), extra.begin(), extra.end());
        }
        return result;
    }
    bool parallel(Point a, Point b) const {
        return a.norm() > 1e-10 && b.norm() > 1e-10 &&
               std::abs(cross(a, b)) <= a.norm() * b.norm() *
                   std::sin(std::clamp(opt_.angle_tol_deg, 0.0, 45.0) * radians);
    }
    bool exact(double a, double b) const {
        return std::abs(a - b) <= 1e-9 + 1e-10 * std::max(std::abs(a), std::abs(b));
    }
    Point direction(const json& e) const { return point(e.at("p2")) - point(e.at("p1")); }
    bool axis_aligned(const json& e) const {
        Point d = direction(e);
        return parallel(d, {1, 0}) || parallel(d, {0, 1});
    }
    bool on_curve(Point p, const json& e) const {
        if (line_curve(e)) {
            Point a = point(e.at("p1")), d = direction(e);
            if (d.norm() < 1e-10) return false;
            double t = dot(p - a, d) / dot(d, d);
            return t >= 0 && t <= 1 && std::abs(cross(d, p - a)) / d.norm() <= opt_.length_tol;
        }
        if (!round_curve(e)) return false;
        Point d = p - point(e.at("center"));
        if (std::abs(d.norm() - e.at("radius").get<double>()) > opt_.length_tol) return false;
        if (e.at("type") == "circle") return true;
        auto positive = [](double a) { a = std::fmod(a, 360.0); return a < 0 ? a + 360 : a; };
        double start = e.at("start"), end = e.at("end");
        double angle = std::atan2(d.y, d.x) / radians;
        bool ccw = e.value("ccw", true);
        double sweep = positive(ccw ? end - start : start - end);
        return positive(ccw ? angle - start : start - angle) <= sweep + opt_.angle_tol_deg;
    }
    bool holds(const json& c) const {
        if (c.contains("value")) return true;  // measured immediately before the trial
        const auto type = c.at("type").get<std::string>();
        const auto refs = c.at("refs").get<std::vector<std::string>>();
        auto geometry = [&](int i) -> const json& { return solved_.entities.at(entity_of(refs[i])); };
        auto p = [&](int i) { return handle_point(refs[i], solved_.entities); };
        if (type == "coincident") return (p(0) - p(1)).norm() <= opt_.length_tol;
        if (type == "horizontal") return parallel(direction(geometry(0)), {1, 0});
        if (type == "vertical") return parallel(direction(geometry(0)), {0, 1});
        if (type == "parallel" || type == "collinear") {
            if (!parallel(direction(geometry(0)), direction(geometry(1)))) return false;
            return type == "parallel" ||
                   std::abs(cross(direction(geometry(0)),
                                  point(geometry(1).at("p1")) - point(geometry(0).at("p1")))) /
                       direction(geometry(0)).norm() <= opt_.length_tol;
        }
        if (type == "perpendicular") {
            Point b = direction(geometry(1));
            return parallel(direction(geometry(0)), {-b.y, b.x});
        }
        if (type == "concentric")
            return (point(geometry(0).at("center")) - point(geometry(1).at("center"))).norm() <= opt_.length_tol;
        if (type == "equal")
            return line_curve(geometry(0))
                ? exact(direction(geometry(0)).norm(), direction(geometry(1)).norm())
                : exact(geometry(0).at("radius"), geometry(1).at("radius"));
        if (type == "midpoint") {
            Point a = point(geometry(1).at("p1")), b = point(geometry(1).at("p2"));
            return (p(0) - Point{(a.x + b.x) / 2, (a.y + b.y) / 2}).norm() <= opt_.length_tol;
        }
        if (type == "point_on") return on_curve(p(0), geometry(1));
        if (type == "tangent") {
            // Explicit arc endpoint refs select the G1 junction, not the singular
            // center-distance tangency equation at an already-coincident endpoint.
            Point junction = line_curve(geometry(0)) ? p(1) : p(0);
            if (line_curve(geometry(0))) {
                if (std::min((junction - point(geometry(0).at("p1"))).norm(),
                             (junction - point(geometry(0).at("p2"))).norm()) > opt_.length_tol)
                    return false;
            } else if ((p(0) - p(1)).norm() > opt_.length_tol) return false;
            Point a = line_curve(geometry(0)) ? direction(geometry(0))
                : junction - point(geometry(0).at("center"));
            Point b = line_curve(geometry(1)) ? direction(geometry(1))
                : junction - point(geometry(1).at("center"));
            if (line_curve(geometry(0)) != line_curve(geometry(1))) b = {-b.y, b.x};
            return parallel(a, b);
        }
        return false;
    }
    void queue(std::vector<json>& candidates, json c) {
        if (holds(c) && considered_.insert(signature(c)).second) candidates.push_back(std::move(c));
    }
    void relation(std::vector<json>& out, const char* type, std::vector<std::string> refs) {
        queue(out, {{"type", type}, {"refs", std::move(refs)}});
    }
    void dimension(std::vector<json>& out, const char* type, std::vector<std::string> refs,
                   Point placement, const char* axis = "") {
        json c = {{"type", type}, {"refs", std::move(refs)}, {"placement", {placement.x, placement.y}}};
        if (*axis) c["axis"] = axis;
        if (auto value = measure_constraint(c, solved_.entities)) {
            if (!std::isfinite(*value)) return;
            c["value"] = {{"value", *value}};
            queue(out, std::move(c));
        }
    }
    SketchSolution solve_trial(const std::vector<json>& candidates) const {
        if (ids_.size() < 32 || candidates.size() > ids_.size() / 4)
            return solve_sketch(sketch_, env_, solved_.state, std::nullopt, true);
        std::set<std::string> touched, proposed_ids;
        for (const auto& c : candidates) {
            proposed_ids.insert(c.at("id").get<std::string>());
            for (const auto& ref : c.at("refs"))
                touched.insert(entity_of(ref.get<std::string>()));
        }
        json local = {{"entities", json::array()}, {"constraints", json::array()}};
        for (const auto& component : components(sketch_)) {
            bool affected = false;
            for (const auto& e : component.at("entities"))
                if (touched.count(e.at("id").get<std::string>())) { affected = true; break; }
            if (!affected) continue;
            for (const auto& e : component.at("entities")) local["entities"].push_back(e);
            for (const auto& c : component.at("constraints")) local["constraints"].push_back(c);
        }
        if (local["entities"].size() == ids_.size())
            return solve_sketch(sketch_, env_, solved_.state, std::nullopt, true);
        // When a batch is split to attribute redundancy, only re-solve affected
        // connected profiles. Re-solving every other island for each rejected
        // candidate turns a 200-entity sketch into hundreds of full-sketch solves.
        json baseline = local;
        auto& constraints = baseline["constraints"];
        constraints.erase(std::remove_if(constraints.begin(), constraints.end(),
            [&](const json& c) { return proposed_ids.count(c.value("id", std::string())); }), constraints.end());
        auto before = solve_sketch(baseline, env_, solved_.state, std::nullopt, true);
        auto after = solve_sketch(local, env_, before.state, std::nullopt, true);
        SketchSolution merged = solved_;
        merged.dof += after.dof - before.dof;
        merged.converged = after.converged;
        merged.conflicting = std::move(after.conflicting);
        merged.redundant = std::move(after.redundant);
        for (const auto& entry : after.var_map) {
            const auto& target = merged.var_map.at(entry.first);
            for (std::size_t i = 0; i < target.size(); ++i)
                merged.x(target[i]) = after.x(entry.second[i]);
            merged.entities[entry.first] = std::move(after.entities[entry.first]);
            merged.statuses[entry.first] = after.statuses.at(entry.first);
        }
        merged.state.x = merged.x;
        return merged;
    }
    void accept(std::vector<json> candidates) {
        if (candidates.empty() || solved_.dof == 0) return;
        const auto count = sketch_["constraints"].size();
        for (auto& c : candidates) {
            if (c.contains("value")) {
                auto value = measure_constraint(c, solved_.entities);
                if (value) c["value"] = {{"value", *value}};
            }
            do { c["id"] = "__fully_define_" + std::to_string(next_id_++); }
            while (constraint_ids_.count(c.at("id").get<std::string>()));
            sketch_["constraints"].push_back(c);
        }
        auto trial = solve_trial(candidates);
        sketch_["constraints"].erase(sketch_["constraints"].begin() + count, sketch_["constraints"].end());
        const bool unchanged = geometry_matches(original_, trial.entities, opt_.length_tol);
        if (trial.converged && trial.conflicting.empty() && trial.redundant.empty() &&
            trial.dof <= solved_.dof - static_cast<int>(candidates.size()) && unchanged) {
            for (auto& c : candidates) {
                constraint_ids_.insert(c.at("id").get<std::string>());
                sketch_["constraints"].push_back(c);
                c.erase("id");
                result_.constraints.push_back(std::move(c));
            }
            solved_ = std::move(trial);
            return;
        }
        if (candidates.size() == 1 ||
            (trial.converged && unchanged && trial.dof >= solved_.dof)) return;
        // One QR can eliminate many dependent dimensions/relations at once. If
        // pivoting instead blames an existing constraint, split chronologically;
        // never remove or replace the user's constraint to make a trial succeed.
        if (trial.converged && trial.conflicting.empty() && unchanged && !trial.redundant.empty()) {
            std::set<std::string> redundant(trial.redundant.begin(), trial.redundant.end());
            std::vector<json> independent;
            for (const auto& c : candidates)
                if (!redundant.count(c.at("id").get<std::string>())) independent.push_back(c);
            if (independent.size() < candidates.size()) {
                accept(std::move(independent));
                return;
            }
        }
        auto middle = candidates.begin() + candidates.size() / 2;
        std::vector<json> right(middle, candidates.end());
        candidates.erase(middle, candidates.end());
        accept(std::move(candidates));
        // Earlier accepted approximate relations may have changed the last few
        // microns. Verify the remaining candidates against that current geometry.
        right.erase(std::remove_if(right.begin(), right.end(),
                    [&](const json& c) { return !holds(c); }), right.end());
        accept(std::move(right));
    }

    using Freedom = std::map<std::string, std::set<std::string>>;
    Freedom freedom() const {
        Freedom free;
        for (const auto& component : components(sketch_)) {
            Eigen::Index size = 0;
            for (const auto& e : component.at("entities"))
                size += solved_.var_map.at(e.at("id").get<std::string>()).size();
            Eigen::VectorXd x(size);
            Eigen::Index offset = 0;
            // Interactive solve component order need not equal authored order.
            for (const auto& e : component.at("entities"))
                for (int index : solved_.var_map.at(e.at("id").get<std::string>()))
                    x(offset++) = solved_.x(index);
            for (const auto& variable : free_variables(component, env_, x))
                free[variable.entity].insert(variable.param);
        }
        return free;
    }
    std::pair<bool, bool> point_freedom(const std::string& handle, const Freedom& free) const {
        const auto id = entity_of(handle);
        auto found = free.find(id);
        if (found == free.end()) return {false, false};
        const auto& params = found->second;
        const auto& e = solved_.entities.at(id);
        const auto type = e.value("type", std::string());
        const auto sub = handle.find('.') == std::string::npos ? "" : handle.substr(handle.find('.') + 1);
        std::string prefix;
        if (type == "spline" || type == "style_spline") {
            std::size_t index = sub == "start" ? 0 : sub == "end"
                ? e.at("points").size() - 1 : std::stoul(sub.substr(1));
            prefix = "point " + std::to_string(index) + " ";
        } else if (sub == "p1" || sub == "p2" || sub == "center") prefix = sub + " ";
        else if (type == "arc" && (sub == "start" || sub == "end")) {
            bool rim = params.count("radius") || params.count(sub + " angle");
            return {rim || params.count("center x"), rim || params.count("center y")};
        }
        return {params.count(prefix + "x") != 0, params.count(prefix + "y") != 0};
    }
    void infer_relations() {
        auto endpoints = this->endpoints();
        std::vector<json> candidates;
        // Equal cap radii express slot intent. Establish them before coincidence
        // and h/v choices can make them numerically dependent at symmetric caps.
        Groups radii;
        for (std::size_t i = 0; i < ids_.size(); ++i)
            for (std::size_t j = i + 1; j < ids_.size(); ++j) {
                const auto& a = solved_.entities.at(ids_[i]);
                const auto& b = solved_.entities.at(ids_[j]);
                if (eligible(ids_[i], ids_[j]) && round_curve(a) && round_curve(b) &&
                    exact(a["radius"], b["radius"]) && radii.join(ids_[i], ids_[j]))
                    relation(candidates, "equal", {ids_[i], ids_[j]});
            }
        accept(std::move(candidates));
        candidates.clear();
        Groups coincident;
        for (const auto& c : sketch_["constraints"])
            if (c.value("type", "") == "coincident" && c.value("refs", json::array()).size() == 2)
                coincident.join(c["refs"][0], c["refs"][1]);
        for (std::size_t i = 0; i < endpoints.size(); ++i) {
            const auto& a = endpoints[i];
            if (opt_.anchor_origin && selected(a.handle) && std::hypot(a.x, a.y) <= opt_.length_tol &&
                coincident.join(a.handle, "origin"))
                relation(candidates, "coincident", {a.handle, "origin"});
            for (std::size_t j = i + 1; j < endpoints.size(); ++j) {
                const auto& b = endpoints[j];
                if (entity_of(a.handle) == entity_of(b.handle) || !eligible(a.handle, b.handle) ||
                    std::hypot(a.x - b.x, a.y - b.y) > opt_.length_tol) continue;
                // Use the human-readable concentric relation for two centers.
                if (a.handle.find(".center") != std::string::npos &&
                    b.handle.find(".center") != std::string::npos) continue;
                if (coincident.join(a.handle, b.handle))
                    relation(candidates, "coincident", {a.handle, b.handle});
            }
        }
        accept(std::move(candidates));
        candidates.clear();
        Groups collinear;
        for (std::size_t i = 0; i < ids_.size(); ++i)
            for (std::size_t j = i + 1; j < ids_.size(); ++j) {
                const auto& a = solved_.entities.at(ids_[i]);
                const auto& b = solved_.entities.at(ids_[j]);
                if (eligible(ids_[i], ids_[j]) && line_curve(a) && line_curve(b)) {
                    json c = {{"type", "collinear"}, {"refs", {ids_[i], ids_[j]}}};
                    if (holds(c) && collinear.join(ids_[i], ids_[j])) queue(candidates, std::move(c));
                }
            }
        accept(std::move(candidates));
        candidates.clear();
        for (const auto& id : ids_) {
            const auto& e = solved_.entities.at(id);
            if (!selected(id) || !line_curve(e)) continue;
            if (parallel(direction(e), {1, 0})) relation(candidates, "horizontal", {id});
            else if (parallel(direction(e), {0, 1})) relation(candidates, "vertical", {id});
        }
        accept(std::move(candidates));
        candidates.clear();
        // Only real rim endpoints form tangent junctions; centers never do.
        for (std::size_t i = 0; i < endpoints.size(); ++i) {
            const auto& a = endpoints[i];
            if (a.handle.find(".center") != std::string::npos) continue;
            for (std::size_t j = i + 1; j < endpoints.size(); ++j) {
                const auto& b = endpoints[j];
                auto ai = entity_of(a.handle), bi = entity_of(b.handle);
                if (ai == bi || !eligible(ai, bi) || b.handle.find(".center") != std::string::npos ||
                    (handle_point(a.handle, solved_.entities) - handle_point(b.handle, solved_.entities)).norm() >
                        opt_.length_tol) continue;
                const auto& ae = solved_.entities.at(ai);
                const auto& be = solved_.entities.at(bi);
                if (line_curve(ae) && be.value("type", "") == "arc")
                    relation(candidates, "tangent", {ai, b.handle});
                else if (ae.value("type", "") == "arc" && line_curve(be))
                    relation(candidates, "tangent", {bi, a.handle});
                else if (ae.value("type", "") == "arc" && be.value("type", "") == "arc")
                    relation(candidates, "tangent", {a.handle, b.handle});
            }
        }
        accept(std::move(candidates));
        candidates.clear();
        struct LinePair { std::string a, b; bool perpendicular; double score; };
        std::vector<LinePair> pairs;
        Groups orientation;
        for (const auto& c : sketch_["constraints"]) {
            auto type = c.value("type", std::string());
            auto refs = c.value("refs", json::array());
            if ((type == "horizontal" || type == "vertical") && refs.size() == 1)
                orientation.join(refs[0], "__world_orientation");
            else if ((type == "parallel" || type == "perpendicular" || type == "collinear") && refs.size() == 2)
                orientation.join(refs[0], refs[1]);
        }
        for (std::size_t i = 0; i < ids_.size(); ++i) {
            const auto& a = solved_.entities.at(ids_[i]);
            if (!line_curve(a)) continue;
            for (std::size_t j = i + 1; j < ids_.size(); ++j) {
                const auto& b = solved_.entities.at(ids_[j]);
                if (!line_curve(b) || !eligible(ids_[i], ids_[j])) continue;
                Point bd = direction(b);
                bool perp = parallel(direction(a), {-bd.y, bd.x});
                if (!perp && !parallel(direction(a), bd)) continue;
                double distance = std::min({
                    (point(a["p1"]) - point(b["p1"])).norm(), (point(a["p1"]) - point(b["p2"])).norm(),
                    (point(a["p2"]) - point(b["p1"])).norm(), (point(a["p2"]) - point(b["p2"])).norm()});
                pairs.push_back({ids_[i], ids_[j], perp,
                                 (axis_aligned(a) || axis_aligned(b) ? 0.0 : 1e12) + distance});
            }
        }
        std::stable_sort(pairs.begin(), pairs.end(), [](const LinePair& a, const LinePair& b) {
            return a.score < b.score;
        });
        for (const auto& pair : pairs)
            if (orientation.join(pair.a, pair.b))
                relation(candidates, pair.perpendicular ? "perpendicular" : "parallel", {pair.a, pair.b});
        accept(std::move(candidates));
        candidates.clear();
        Groups equal, centers;
        for (std::size_t i = 0; i < ids_.size(); ++i) {
            const auto& a = solved_.entities.at(ids_[i]);
            for (std::size_t j = i + 1; j < ids_.size(); ++j) {
                const auto& b = solved_.entities.at(ids_[j]);
                if (!eligible(ids_[i], ids_[j])) continue;
                if ((line_curve(a) && line_curve(b) && exact(direction(a).norm(), direction(b).norm())) ||
                    (round_curve(a) && round_curve(b) && exact(a["radius"], b["radius"]))) {
                    if (equal.join(ids_[i], ids_[j])) relation(candidates, "equal", {ids_[i], ids_[j]});
                }
                if (round_curve(a) && round_curve(b) &&
                    (point(a["center"]) - point(b["center"])).norm() <= opt_.length_tol &&
                    centers.join(ids_[i], ids_[j]))
                    relation(candidates, "concentric", {ids_[i], ids_[j]});
            }
        }
        accept(std::move(candidates));
        candidates.clear();
        for (const auto& ep : endpoints) {
            const Point current = handle_point(ep.handle, solved_.entities);
            for (const auto& id : ids_) {
                if (entity_of(ep.handle) == id || !eligible(ep.handle, id)) continue;
                const auto& e = solved_.entities.at(id);
                if (!line_curve(e)) continue;
                const Point a = point(e["p1"]), b = point(e["p2"]);
                if ((current - Point{(a.x + b.x) / 2, (a.y + b.y) / 2}).norm() <= opt_.length_tol)
                    relation(candidates, "midpoint", {ep.handle, id});
            }
        }
        accept(std::move(candidates));
        candidates.clear();
        Groups same_support;
        for (const auto& c : sketch_["constraints"])
            if (c.value("type", "") == "collinear" && c.value("refs", json::array()).size() == 2)
                same_support.join(c["refs"][0], c["refs"][1]);
        for (const auto& ep : endpoints) {
            std::set<std::string> targets;
            Point current = handle_point(ep.handle, solved_.entities);
            for (const auto& id : ids_) {
                if (entity_of(ep.handle) == id || !eligible(ep.handle, id)) continue;
                const auto& e = solved_.entities.at(id);
                if (!line_curve(e) && !round_curve(e)) continue;
                if (!on_curve(current, e)) continue;
                // A coincident rim/line endpoint already implies point_on.
                bool at_endpoint = false;
                for (const auto& other : endpoints)
                    if (entity_of(other.handle) == id &&
                        other.handle.find(".center") == std::string::npos &&
                        (current - handle_point(other.handle, solved_.entities)).norm() <= opt_.length_tol) {
                        at_endpoint = true;
                        break;
                    }
                if (at_endpoint) continue;
                if (line_curve(e)) {
                    const auto support = same_support.root(id);
                    if ((line_curve(solved_.entities.at(entity_of(ep.handle))) &&
                         support == same_support.root(entity_of(ep.handle))) || targets.count(support)) continue;
                    json c = {{"type", "point_on"}, {"refs", {ep.handle, id}}};
                    if (holds(c)) { queue(candidates, std::move(c)); targets.insert(support); }
                } else relation(candidates, "point_on", {ep.handle, id});
            }
        }
        accept(std::move(candidates));
    }

    void add_dimensions() {
        auto free = freedom();
        std::vector<json> candidates;
        for (const auto& id : ids_) {
            if (!selected(id) || !free.count(id)) continue;
            const auto& e = solved_.entities.at(id);
            if (round_curve(e) && free[id].count("radius")) {
                Point c = point(e["center"]);
                double r = e["radius"];
                dimension(candidates, e["type"] == "circle" ? "diameter" : "radius", {id},
                          {c.x + r * 0.8 + 3, c.y + r * 0.8 + 3});
            } else if (line_curve(e)) {
                Point a = point(e["p1"]), b = point(e["p2"]), d = direction(e);
                double offset = std::max(3.0, d.norm() * 0.12);
                if (d.norm() > 1e-10)
                    dimension(candidates, "distance", {id},
                              {(a.x + b.x) / 2 - d.y / d.norm() * offset,
                               (a.y + b.y) / 2 + d.x / d.norm() * offset});
            }
        }
        accept(std::move(candidates));
        candidates.clear();
        free = freedom();
        std::set<std::string> orientation_fixed;
        for (const auto& c : sketch_["constraints"])
            if ((c.value("type", "") == "horizontal" || c.value("type", "") == "vertical") &&
                c.value("refs", json::array()).size() == 1)
                orientation_fixed.insert(c["refs"][0].get<std::string>());
        for (const auto& id : ids_) {
            const auto& e = solved_.entities.at(id);
            if (!selected(id) || !free.count(id) || !line_curve(e) || orientation_fixed.count(id)) continue;
            Point a = point(e["p1"]);
            dimension(candidates, "angle", {"axis.h", id}, {a.x + 6, a.y + 6});
        }
        accept(std::move(candidates));
        candidates.clear();

        free = freedom();
        auto endpoints = this->endpoints();
        // Prefer an existing fixed point as datum when origin anchoring is disabled.
        std::string datum = opt_.anchor_origin ? "origin" : "";
        if (datum.empty()) {
            for (const auto& ep : endpoints) {
                auto loose = point_freedom(ep.handle, free);
                if (!loose.first && !loose.second) { datum = ep.handle; break; }
            }
            if (datum.empty() && !endpoints.empty()) datum = endpoints.front().handle;
        }
        auto locate = [&](const Endpoint& ep, const std::string& reference, const Freedom& loose) {
            if (reference.empty() || !selected(ep.handle) || !loose.count(entity_of(ep.handle))) return;
            // start/end and pK can name the identical spline variable pair.
            // A dimension to that same point must not anchor a floating datum.
            if (canonical_point_handle(ep.handle, solved_.entities) ==
                canonical_point_handle(reference, solved_.entities)) return;
            Point p = handle_point(ep.handle, solved_.entities), a = handle_point(reference, solved_.entities);
            const auto axes = point_freedom(ep.handle, loose);
            if (axes.first)
                dimension(candidates, "distance", {reference, ep.handle},
                          {(a.x + p.x) / 2, p.y - 5}, "h");
            if (axes.second)
                dimension(candidates, "distance", {reference, ep.handle},
                          {p.x + 5, (a.y + p.y) / 2}, "v");
        };
        // One location pair per connected profile avoids chains of dimensions
        // around a rectangle; independent islands can be verified in one batch.
        for (const auto& component : components(sketch_)) {
            bool found = false;
            for (const auto& e : component.at("entities")) {
                auto id = e.at("id").get<std::string>();
                if (!selected(id) || !free.count(id)) continue;
                for (const auto& ep : endpoints)
                    if (entity_of(ep.handle) == id) {
                        locate(ep, datum, free);
                        found = true;
                        break;
                    }
                if (found) break;
            }
        }
        accept(std::move(candidates));
        candidates.clear();
        if (solved_.dof == 0) return;
        free = freedom();
        // A free arc sweep/rotation needs rim dimensions rather than more center
        // coordinates. Relative-to-center dimensions remain valid without origin.
        for (const auto& ep : endpoints) {
            auto id = entity_of(ep.handle);
            const auto& e = solved_.entities.at(id);
            std::string reference = e["type"] == "arc" && ep.handle != id + ".center"
                ? id + ".center" : datum;
            locate(ep, reference, free);
        }
        accept(std::move(candidates));
        if (solved_.dof == 0) return;
        const bool has_spline = std::any_of(ids_.begin(), ids_.end(), [&](const std::string& id) {
            const auto type = solved_.entities.at(id).value("type", std::string());
            return selected(id) && (type == "spline" || type == "style_spline");
        });
        if (!has_spline) return;
        candidates.clear();
        free = freedom();
        // Through-points are normal editable sketch geometry, not opaque spline
        // internals. Dimension only their still-free coordinates, in point order.
        for (const auto& id : ids_) {
            const auto& e = solved_.entities.at(id);
            const auto type = e.value("type", std::string());
            if (!selected(id) || !free.count(id) || (type != "spline" && type != "style_spline")) continue;
            for (std::size_t k = 0; k < e.at("points").size(); ++k) {
                const auto p = point(e.at("points")[k]);
                locate({id + ".p" + std::to_string(k), p.x, p.y}, datum, free);
            }
        }
        accept(std::move(candidates));
    }

public:
    Definer(const json& sketch, const json& env, const DefineOptions& opt)
        : env_(env), opt_(opt), sketch_(sketch),
          selected_(opt.only_entities.begin(), opt.only_entities.end()) {
        if (!sketch_.contains("entities")) sketch_["entities"] = json::array();
        if (!sketch_.contains("constraints")) sketch_["constraints"] = json::array();
        for (const auto& e : sketch_["entities"]) ids_.push_back(e.at("id").get<std::string>());
        for (const auto& c : sketch_["constraints"]) {
            if (c.contains("id")) constraint_ids_.insert(c.at("id").get<std::string>());
            if (!c.value("driven", false)) considered_.insert(signature(c));
        }
    }
    DefineResult run() {
        solved_ = solve_sketch(sketch_, env_, std::nullopt, std::nullopt, true);
        result_.dof_before = result_.dof_after = solved_.dof;
        if (!solved_.converged || !solved_.conflicting.empty() || !solved_.redundant.empty()) {
            result_.notes.push_back("Resolve existing conflicting or redundant constraints before fully defining the sketch.");
            return result_;
        }
        if (solved_.dof == 0) return result_;
        original_ = solved_.entities;
        if (opt_.relations) infer_relations();
        if (opt_.dimensions && solved_.dof > 0) add_dimensions();
        result_.dof_after = solved_.dof;
        if (solved_.dof > 0) {
            auto free = freedom();
            for (const auto& id : ids_) {
                if (!selected(id) || !free.count(id)) continue;
                const auto type = solved_.entities.at(id).value("type", std::string());
                if (type != "line" && type != "circle" && type != "arc" && type != "point" &&
                    type != "spline" && type != "style_spline")
                    result_.notes.push_back(type + " " + id + " has free parameters without supported driving dimensions.");
                else {
                    std::string names;
                    for (const auto& param : free.at(id)) {
                        if (!names.empty()) names += ", ";
                        names += param;
                    }
                    result_.notes.push_back(type + " " + id + " left free: " + names + ".");
                }
            }
            if (!opt_.anchor_origin)
                result_.notes.push_back("Origin anchoring disabled; unconstrained global placement is retained.");
            if (!selected_.empty()) result_.notes.push_back("Only the selected entities were considered.");
            result_.notes.push_back(std::to_string(solved_.dof) + " degrees of freedom remain.");
        }
        return result_;
    }
};
}  // namespace

DefineResult fully_define(const json& sketch, const json& env, const DefineOptions& opt) {
    return Definer(sketch, env, opt).run();
}
}  // namespace chudware::sketch
