// CamMirror — parse cam_tree / cam_generate JSON into typed view + classified backplot.
#include "cam_mirror.h"

#include <cmath>
#include <cstdint>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#include "../../mesh.h"   // Mesh — camFieldToMesh output

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
            if (s.contains("machine_detail") && rapidXY == 5000 && rapidZ == 3000) {
                rapidXY = jnum(s["machine_detail"], "rapid_xy", rapidXY);
                rapidZ = jnum(s["machine_detail"], "rapid_z", rapidZ);
            }
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
    double elapsed = 0;   // cumulative seconds (mirrors the engine's feed-limited model)
    auto bump = [&](const std::array<float, 3>& p) {
        if (first) { bp_.minx = bp_.maxx = p[0]; bp_.miny = bp_.maxy = p[1]; bp_.minz = bp_.maxz = p[2]; first = false; }
        else {
            bp_.minx = std::min(bp_.minx, p[0]); bp_.maxx = std::max(bp_.maxx, p[0]);
            bp_.miny = std::min(bp_.miny, p[1]); bp_.maxy = std::max(bp_.maxy, p[1]);
            bp_.minz = std::min(bp_.minz, p[2]); bp_.maxz = std::max(bp_.maxz, p[2]);
        }
    };
    auto seg = [&](std::vector<float>& v, std::vector<float>& tv,
                   const std::array<float, 3>& a, const std::array<float, 3>& b) {
        v.push_back(a[0]); v.push_back(a[1]); v.push_back(a[2]);
        v.push_back(b[0]); v.push_back(b[1]); v.push_back(b[2]);
        tv.push_back((float)elapsed);
        bump(a); bump(b);
    };
    auto feed_dt = [&](const std::array<float, 3>& a, const std::array<float, 3>& b,
                       double feed) -> double {
        double dist = std::sqrt((b[0]-a[0])*(b[0]-a[0]) + (b[1]-a[1])*(b[1]-a[1]) +
                                (b[2]-a[2])*(b[2]-a[2]));
        double f = feed > 0 ? feed : 600;
        return 60.0 * dist / f;
    };
    auto rapid_dt = [&](const std::array<float, 3>& a, const std::array<float, 3>& b) -> double {
        double dxy = std::hypot(b[0]-a[0], b[1]-a[1]);
        double dz = std::fabs(b[2]-a[2]);
        return 60.0 * std::max(dxy / rapidXY, dz / rapidZ);
    };
    const nlohmann::json& tps = resp.contains("toolpaths") ? resp["toolpaths"] : resp;
    if (!tps.is_array()) return;
    for (const auto& tp : tps) {
        if (!tp.contains("moves")) continue;
        std::array<float, 3> prev{0, 0, 0};
        bool have = false;
        for (const auto& m : tp["moves"]) {
            std::string kind = jstr(m, "kind", "cut");
            if (kind == "dwell" || kind == "tool_change") {
                elapsed += jnum(m, "dwell_s", 0);
                continue;
            }
            std::array<float, 3> to = m.contains("to") ? jvec3(m["to"]) : std::array<float, 3>{0, 0, 0};
            if (!have) { prev = to; have = true; continue; }
            if (kind == "rapid" || kind == "retract") {
                elapsed += rapid_dt(prev, to);
                seg(bp_.rapid, bp_.rapidT, prev, to);
            } else if (kind == "lead") {
                elapsed += feed_dt(prev, to, jnum(m, "feed", 0));
                seg(bp_.lead, bp_.leadT, prev, to);
            } else if (kind == "plunge") {
                elapsed += feed_dt(prev, to, jnum(m, "feed", 0));
                seg(bp_.plunge, bp_.plungeT, prev, to);
            } else if (kind == "arc") {
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
                double dt = feed_dt(prev, to, jnum(m, "feed", 0)) / n;
                std::array<float, 3> p = prev;
                for (int i = 1; i <= n; ++i) {
                    double a = a0 + sweep * i / n;
                    float z = (float)(prev[2] + (to[2] - prev[2]) * (double)i / n);
                    std::array<float, 3> q{(float)(c[0] + r * std::cos(a)), (float)(c[1] + r * std::sin(a)), z};
                    elapsed += dt;
                    seg(bp_.arc, bp_.arcT, p, q);
                    p = q;
                }
            } else {
                elapsed += feed_dt(prev, to, jnum(m, "feed", 0));
                seg(bp_.cut, bp_.cutT, prev, to);   // cut + anything else
            }
            prev = to;
        }
    }
    bp_.totalTime = elapsed;
}

namespace {
void split_class(const std::vector<float>& segs, const std::vector<float>& times, double t,
                 std::vector<float>& before, std::vector<float>& after) {
    for (size_t i = 0; i + 5 < segs.size(); i += 6) {
        double tEnd = (i / 6 < times.size()) ? times[i / 6] : 0;
        double tStart = (i >= 6) ? times[i / 6 - 1] : 0.0;
        if (tEnd <= t) {
            for (int k = 0; k < 6; ++k) before.push_back(segs[i + k]);
        } else if (tStart < t && t < tEnd) {
            // straddling segment: clip at the interpolated point
            double f = (tEnd > tStart) ? (t - tStart) / (tEnd - tStart) : 0.0;
            float mx = segs[i] + (segs[i+3] - segs[i]) * (float)f;
            float my = segs[i+1] + (segs[i+4] - segs[i+1]) * (float)f;
            float mz = segs[i+2] + (segs[i+5] - segs[i+2]) * (float)f;
            before.push_back(segs[i]); before.push_back(segs[i+1]); before.push_back(segs[i+2]);
            before.push_back(mx); before.push_back(my); before.push_back(mz);
            after.push_back(mx); after.push_back(my); after.push_back(mz);
            after.push_back(segs[i+3]); after.push_back(segs[i+4]); after.push_back(segs[i+5]);
        } else {
            for (int k = 0; k < 6; ++k) after.push_back(segs[i + k]);
        }
    }
}
}  // namespace

void CamBackplot::splitAt(double t, CamBackplot& before, CamBackplot& after) const {
    before.clear(); after.clear();
    split_class(rapid, rapidT, t, before.rapid, after.rapid);
    split_class(lead, leadT, t, before.lead, after.lead);
    split_class(plunge, plungeT, t, before.plunge, after.plunge);
    split_class(cut, cutT, t, before.cut, after.cut);
    split_class(arc, arcT, t, before.arc, after.arc);
    before.minx = minx; before.maxx = maxx; before.miny = miny; before.maxy = maxy;
    before.minz = minz; before.maxz = maxz;
    after.minx = minx; after.maxx = maxx; after.miny = miny; after.maxy = maxy;
    after.minz = minz; after.maxz = maxz;
    before.totalTime = totalTime; after.totalTime = totalTime;
}

bool camFieldToMesh(const nlohmann::json& field, Mesh& out) {
    if (!field.contains("nx") || !field.contains("ny") || !field.contains("z") ||
        !field.contains("lo") || !field.contains("res")) return false;
    int nx = field["nx"].get<int>();
    int ny = field["ny"].get<int>();
    double res = field["res"].get<double>();
    const auto& lo = field["lo"];
    const auto& z = field["z"];
    if (nx < 2 || ny < 2 || res <= 0 || !lo.is_array() || lo.size() < 2 ||
        !z.is_array() || (int)z.size() < nx * ny) return false;
    int step = (nx * ny > 250000) ? 2 : 1;
    int gx = (nx + step - 1) / step, gy = (ny + step - 1) / step;
    double lox = lo[0].get<double>(), loy = lo[1].get<double>();
    auto zat = [&](int i, int j) -> float {
        i = std::max(0, std::min(nx - 1, i));
        j = std::max(0, std::min(ny - 1, j));
        return z[(size_t)j * nx + i].get<float>();
    };
    out.positions.clear(); out.normals.clear(); out.indices.clear();
    out.positions.reserve((size_t)gx * gy * 3);
    out.normals.reserve((size_t)gx * gy * 3);
    out.indices.reserve((size_t)(gx - 1) * (gy - 1) * 6);
    for (int j = 0; j < gy; ++j)
        for (int i = 0; i < gx; ++i) {
            int ii = i * step, jj = j * step;
            float px = (float)(lox + ii * res), py = (float)(loy + jj * res);
            out.positions.push_back(px); out.positions.push_back(py);
            out.positions.push_back(zat(ii, jj));
        }
    for (int j = 0; j < gy; ++j)
        for (int i = 0; i < gx; ++i) {
            int ii = i * step, jj = j * step;
            float zL = zat(ii - step, jj), zR = zat(ii + step, jj);
            float zD = zat(ii, jj - step), zU = zat(ii, jj + step);
            float r = (float)(res * step);
            // central differences: normal = normalize(-dz/dx, -dz/dy, 1)
            float nxv = (zL - zR) / (2 * r), nyv = (zD - zU) / (2 * r), nzv = 1.0f;
            float len = std::sqrt(nxv * nxv + nyv * nyv + nzv * nzv);
            if (len < 1e-9f) { nxv = 0; nyv = 0; nzv = 1; len = 1; }
            out.normals.push_back(nxv / len); out.normals.push_back(nyv / len);
            out.normals.push_back(nzv / len);
        }
    for (int j = 0; j < gy - 1; ++j)
        for (int i = 0; i < gx - 1; ++i) {
            uint32_t a = (uint32_t)(j * gx + i), b = a + 1;
            uint32_t c = a + (uint32_t)gx, d = c + 1;
            out.indices.push_back(a); out.indices.push_back(c); out.indices.push_back(b);
            out.indices.push_back(b); out.indices.push_back(c); out.indices.push_back(d);
        }
    return true;
}

}  // namespace ui
