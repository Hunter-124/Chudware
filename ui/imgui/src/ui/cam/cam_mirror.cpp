// CamMirror — parse cam_tree / cam_generate JSON into typed view + classified backplot.
// Ported verbatim from host/src/ui/cam/cam_mirror.cpp (H1) — pure JSON parsing, no engine
// or ImGui dependency, so the logic carries over unchanged.
#include "cam_mirror.h"

#include <cmath>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace ui {

namespace {
double jnum(const nlohmann::json& j, const char* k, double d = 0) {
    auto it = j.find(k);
    return (it != j.end() && it->is_number()) ? it->get<double>() : d;
}
std::string jstr(const nlohmann::json& j, const char* k, const char* d = "") {
    auto it = j.find(k);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string(d);
}
std::array<float, 3> jvec3(const nlohmann::json& a) {
    if (a.is_array() && a.size() >= 3)
        return {(float)a[0].get<double>(), (float)a[1].get<double>(), (float)a[2].get<double>()};
    return {0, 0, 0};
}
}  // namespace

void CamMirror::loadTree(const nlohmann::json& resp) {
    setups_.clear();
    tools_.clear();
    if (resp.contains("revision") && resp["revision"].is_number())
        revision_ = resp["revision"].get<int>();
    const nlohmann::json& tree = resp.contains("tree") ? resp["tree"] : resp;
    name_ = jstr(tree, "name");
    post_ = jstr(tree, "post", "grbl");
    if (tree.contains("tools") && tree["tools"].is_array())
        for (const auto& t : tree["tools"]) {
            CamToolRow r;
            r.number = (int)jnum(t, "number");
            r.name = jstr(t, "name");
            r.type = jstr(t, "type");
            r.diameter = jnum(t, "diameter");
            tools_.push_back(std::move(r));
        }
    if (tree.contains("setups") && tree["setups"].is_array())
        for (const auto& s : tree["setups"]) {
            CamSetupRow row;
            row.id = jstr(s, "id");
            row.name = jstr(s, "name");
            row.machine = jstr(s, "machine");
            if (s.contains("stock")) row.stock = s["stock"];
            if (s.contains("operations") && s["operations"].is_array())
                for (const auto& o : s["operations"]) {
                    CamOpRow op;
                    op.id = jstr(o, "id");
                    op.name = jstr(o, "name");
                    op.type = jstr(o, "type");
                    op.tool_number = (int)jnum(o, "tool_number");
                    op.suppressed = o.value("suppressed", false);
                    row.ops.push_back(std::move(op));
                }
            setups_.push_back(std::move(row));
        }
}

void CamMirror::loadToolpaths(const nlohmann::json& resp) {
    bp_.clear();
    bool first = true;
    auto bump = [&](const std::array<float, 3>& p) {
        if (first) { bp_.minx = bp_.maxx = p[0]; bp_.miny = bp_.maxy = p[1]; bp_.minz = bp_.maxz = p[2]; first = false; }
        else {
            bp_.minx = std::min(bp_.minx, p[0]); bp_.maxx = std::max(bp_.maxx, p[0]);
            bp_.miny = std::min(bp_.miny, p[1]); bp_.maxy = std::max(bp_.maxy, p[1]);
            bp_.minz = std::min(bp_.minz, p[2]); bp_.maxz = std::max(bp_.maxz, p[2]);
        }
    };
    auto seg = [&](std::vector<float>& v, const std::array<float, 3>& a, const std::array<float, 3>& b) {
        v.push_back(a[0]); v.push_back(a[1]); v.push_back(a[2]);
        v.push_back(b[0]); v.push_back(b[1]); v.push_back(b[2]);
        bump(a); bump(b);
    };
    const nlohmann::json& tps = resp.contains("toolpaths") ? resp["toolpaths"] : resp;
    if (!tps.is_array()) return;
    for (const auto& tp : tps) {
        if (!tp.contains("moves")) continue;
        std::array<float, 3> prev{0, 0, 0};
        bool have = false;
        for (const auto& m : tp["moves"]) {
            std::string kind = jstr(m, "kind", "cut");
            if (kind == "dwell" || kind == "tool_change") continue;
            std::array<float, 3> to = m.contains("to") ? jvec3(m["to"]) : std::array<float, 3>{0, 0, 0};
            if (!have) { prev = to; have = true; continue; }
            if (kind == "rapid" || kind == "retract") seg(bp_.rapid, prev, to);
            else if (kind == "lead") seg(bp_.lead, prev, to);
            else if (kind == "plunge") seg(bp_.plunge, prev, to);
            else if (kind == "arc") {
                // tessellate the arc (center + ccw) into ~5deg chords for display
                std::array<float, 3> c = m.contains("center") ? jvec3(m["center"]) : prev;
                bool ccw = m.value("ccw", false);
                double r = std::hypot(prev[0] - c[0], prev[1] - c[1]);
                double a0 = std::atan2(prev[1] - c[1], prev[0] - c[0]);
                double a1 = std::atan2(to[1] - c[1], to[0] - c[0]);
                double sweep = a1 - a0;
                if (ccw) { while (sweep <= 0) sweep += 2 * M_PI; }
                else { while (sweep >= 0) sweep -= 2 * M_PI; }
                int n = std::max(2, (int)std::ceil(std::fabs(sweep) / (5.0 * M_PI / 180.0)));
                std::array<float, 3> p = prev;
                for (int i = 1; i <= n; ++i) {
                    double a = a0 + sweep * i / n;
                    float z = (float)(prev[2] + (to[2] - prev[2]) * (double)i / n);
                    std::array<float, 3> q{(float)(c[0] + r * std::cos(a)), (float)(c[1] + r * std::sin(a)), z};
                    seg(bp_.arc, p, q);
                    p = q;
                }
            } else seg(bp_.cut, prev, to);  // cut + anything else
            prev = to;
        }
    }
}

}  // namespace ui
