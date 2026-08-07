// CamMirror — parse cam_tree / cam_generate JSON into typed view + classified backplot.
#include "cam_mirror.h"
#include <algorithm>

#include <cmath>
#include <cstdint>
#include <exception>
#include <utility>
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
    if (a.is_array() && a.size() >= 3 &&
        a[0].is_number() && a[1].is_number() && a[2].is_number())
        return {(float)a[0].get<double>(), (float)a[1].get<double>(),
                (float)a[2].get<double>()};
    return {0, 0, 0};
}
}  // namespace

void CamMirror::loadTree(const nlohmann::json& resp) {
    setups_.clear();
    tools_.clear();
    bp_.clear();
    rapidXY = 5000;
    rapidZ = 3000;
    revision_ = (resp.contains("revision") && resp["revision"].is_number())
                    ? resp["revision"].get<int>() : -1;
    const nlohmann::json& tree = resp.contains("tree") ? resp["tree"] : resp;
    name_ = jstr(tree, "name");
    post_ = jstr(tree, "post", "grbl");
    if (tree.contains("tools") && tree["tools"].is_array()) {
        for (const auto& t : tree["tools"]) {
            CamToolRow r;
            r.number = (int)jnum(t, "number");
            r.name = jstr(t, "name");
            r.type = jstr(t, "type");
            r.diameter = jnum(t, "diameter");
            tools_.push_back(std::move(r));
        }
    }
    if (!tree.contains("setups") || !tree["setups"].is_array()) return;
    bool haveRapidRates = false;
    for (const auto& s : tree["setups"]) {
        CamSetupRow row;
        row.id = jstr(s, "id");
        row.name = jstr(s, "name");
        if (s.contains("machine") && s["machine"].is_object())
            row.machine = jstr(s["machine"], "kind");
        else
            row.machine = jstr(s, "machine");
        if (!haveRapidRates && s.contains("machine_detail") && s["machine_detail"].is_object()) {
            rapidXY = jnum(s["machine_detail"], "rapid_xy", rapidXY);
            rapidZ = jnum(s["machine_detail"], "rapid_z", rapidZ);
            haveRapidRates = true;
        }
        if (s.contains("stock")) row.stock = s["stock"];
        if (row.stock.is_object() && row.stock.contains("min") && row.stock.contains("max")) {
            const auto& mn = row.stock["min"];
            const auto& mx = row.stock["max"];
            if (mn.is_array() && mn.size() >= 3 && mx.is_array() && mx.size() >= 3 &&
                mn[0].is_number() && mn[1].is_number() && mn[2].is_number() &&
                mx[0].is_number() && mx[1].is_number() && mx[2].is_number()) {
                const float x0 = mn[0].get<float>(), y0 = mn[1].get<float>(), z0 = mn[2].get<float>();
                const float x1 = mx[0].get<float>(), y1 = mx[1].get<float>(), z1 = mx[2].get<float>();
                const float c[8][3] = {{x0,y0,z0},{x1,y0,z0},{x1,y1,z0},{x0,y1,z0},
                                       {x0,y0,z1},{x1,y0,z1},{x1,y1,z1},{x0,y1,z1}};
                const int e[12][2] = {{0,1},{1,2},{2,3},{3,0},{4,5},{5,6},{6,7},{7,4},
                                      {0,4},{1,5},{2,6},{3,7}};
                row.stockSegments.reserve(12 * 6);
                for (const auto& edge : e)
                    for (int end = 0; end < 2; ++end)
                        row.stockSegments.insert(row.stockSegments.end(),
                                                 c[edge[end]], c[edge[end]] + 3);
            }
        }
        if (s.contains("operations") && s["operations"].is_array()) {
            for (const auto& o : s["operations"]) {
                CamOpRow op;
                op.id = jstr(o, "id");
                op.name = jstr(o, "name");
                op.type = jstr(o, "type");
                op.tool_number = (int)jnum(o, "tool_number");
                op.suppressed = o.value("suppressed", false);
                const nlohmann::json* params =
                    (o.contains("params") && o["params"].is_object()) ? &o["params"] : nullptr;
                auto opnum = [&](const char* key, double fallback = 0.0) {
                    if (o.contains(key) && o[key].is_number()) return o[key].get<double>();
                    return params ? jnum(*params, key, fallback) : fallback;
                };
                op.stepover = opnum("stepover");
                op.stepdown = opnum("stepdown");
                op.stock_leave = opnum("stock_to_leave", opnum("stock_leave"));
                op.feed = opnum("feed");
                op.rpm = opnum("rpm");
                if (o.contains("heights") && o["heights"].is_object()) {
                    const auto& h = o["heights"];
                    op.top = jnum(h, "top");
                    op.bottom = jnum(h, "bottom");
                    op.clearance = jnum(h, "clearance");
                    op.retract = jnum(h, "retract");
                }
                row.ops.push_back(std::move(op));
            }
        }
        setups_.push_back(std::move(row));
    }
}

void CamMirror::loadToolpaths(const nlohmann::json& resp) {
    bp_.clear();
    bool first = true;
    double elapsed = 0.0;
    auto bump = [&](const std::array<float, 3>& p) {
        if (first) {
            bp_.minx = bp_.maxx = p[0];
            bp_.miny = bp_.maxy = p[1];
            bp_.minz = bp_.maxz = p[2];
            first = false;
        } else {
            bp_.minx = std::min(bp_.minx, p[0]); bp_.maxx = std::max(bp_.maxx, p[0]);
            bp_.miny = std::min(bp_.miny, p[1]); bp_.maxy = std::max(bp_.maxy, p[1]);
            bp_.minz = std::min(bp_.minz, p[2]); bp_.maxz = std::max(bp_.maxz, p[2]);
        }
    };
    auto addSegment = [&](std::vector<float>& segs, std::vector<float>& starts,
                          std::vector<float>& ends, const std::array<float, 3>& a,
                          const std::array<float, 3>& b, double dt) {
        segs.insert(segs.end(), a.begin(), a.end());
        segs.insert(segs.end(), b.begin(), b.end());
        starts.push_back((float)elapsed);
        elapsed += std::max(0.0, dt);
        ends.push_back((float)elapsed);
        bump(a);
        bump(b);
    };
    auto feedDt = [](const std::array<float, 3>& a, const std::array<float, 3>& b,
                     double feed) {
        const double dist = std::sqrt((b[0]-a[0])*(b[0]-a[0]) +
                                      (b[1]-a[1])*(b[1]-a[1]) +
                                      (b[2]-a[2])*(b[2]-a[2]));
        return 60.0 * dist / (feed > 0.0 ? feed : 600.0);
    };
    auto rapidDt = [&](const std::array<float, 3>& a, const std::array<float, 3>& b) {
        const double dxy = std::hypot(b[0]-a[0], b[1]-a[1]);
        const double dz = std::fabs(b[2]-a[2]);
        const double xyRate = rapidXY > 0.0 ? rapidXY : 5000.0;
        const double zRate = rapidZ > 0.0 ? rapidZ : 3000.0;
        return 60.0 * std::max(dxy / xyRate, dz / zRate);
    };

    const nlohmann::json& tps = resp.contains("toolpaths") ? resp["toolpaths"] : resp;
    if (!tps.is_array()) return;
    for (const auto& tp : tps) {
        if (!tp.contains("moves") || !tp["moves"].is_array()) continue;
        std::array<float, 3> prev{0, 0, 0};
        bool have = false;
        for (const auto& m : tp["moves"]) {
            const std::string kind = jstr(m, "kind", "cut");
            if (kind == "dwell" || kind == "tool_change") {
                elapsed += std::max(0.0, jnum(m, "dwell_s", 0.0));
                continue;
            }
            if (!m.contains("to") || !m["to"].is_array()) continue;
            const std::array<float, 3> to = jvec3(m["to"]);
            if (!have) {
                prev = to;
                have = true;
                bump(prev);
                continue;
            }
            if (kind == "rapid" || kind == "retract") {
                addSegment(bp_.rapid, bp_.rapidT0, bp_.rapidT, prev, to, rapidDt(prev, to));
            } else if (kind == "lead") {
                addSegment(bp_.lead, bp_.leadT0, bp_.leadT, prev, to,
                           feedDt(prev, to, jnum(m, "feed")));
            } else if (kind == "plunge") {
                addSegment(bp_.plunge, bp_.plungeT0, bp_.plungeT, prev, to,
                           feedDt(prev, to, jnum(m, "feed")));
            } else if (kind == "arc") {
                const std::array<float, 3> c =
                    m.contains("center") ? jvec3(m["center"]) : prev;
                const bool ccw = m.value("ccw", false);
                const double radius = std::hypot(prev[0] - c[0], prev[1] - c[1]);
                const double a0 = std::atan2(prev[1] - c[1], prev[0] - c[0]);
                const double a1 = std::atan2(to[1] - c[1], to[0] - c[0]);
                double sweep = a1 - a0;
                if (ccw) {
                    while (sweep <= 0.0) sweep += 2.0 * M_PI;
                } else {
                    while (sweep >= 0.0) sweep -= 2.0 * M_PI;
                }
                const int count =
                    std::max(2, (int)std::ceil(std::fabs(sweep) / (5.0 * M_PI / 180.0)));
                const double planarLength = radius * std::fabs(sweep);
                const double dz = (double)to[2] - prev[2];
                const double helicalLength = std::hypot(planarLength, dz);
                const double feed = jnum(m, "feed");
                const double dt = 60.0 * helicalLength / (feed > 0.0 ? feed : 600.0) / count;
                std::array<float, 3> p = prev;
                for (int i = 1; i <= count; ++i) {
                    const double a = a0 + sweep * i / count;
                    const float z = (float)(prev[2] + dz * i / count);
                    const std::array<float, 3> q{
                        (float)(c[0] + radius * std::cos(a)),
                        (float)(c[1] + radius * std::sin(a)), z};
                    addSegment(bp_.arc, bp_.arcT0, bp_.arcT, p, q, dt);
                    p = q;
                }
            } else {
                addSegment(bp_.cut, bp_.cutT0, bp_.cutT, prev, to,
                           feedDt(prev, to, jnum(m, "feed")));
            }
            prev = to;
        }
    }
    bp_.totalTime = elapsed;

    auto buildDirections = [](const std::vector<float>& segs, std::vector<float>& dirs) {
        constexpr size_t kMaxTriangles = 128;
        const size_t segmentCount = segs.size() / 6;
        if (segmentCount == 0) return;
        const size_t stride = std::max<size_t>(1, (segmentCount + kMaxTriangles - 1) /
                                                  kMaxTriangles);
        dirs.reserve(((segmentCount + stride - 1) / stride) * 9);
        for (size_t segment = 0; segment < segmentCount; segment += stride) {
            const size_t i = segment * 6;
            const float dx = segs[i+3] - segs[i];
            const float dy = segs[i+4] - segs[i+1];
            const float dz = segs[i+5] - segs[i+2];
            const float length = std::sqrt(dx*dx + dy*dy + dz*dz);
            if (length <= 1e-5f) continue;
            float px = -dy, py = dx, pz = 0.0f;
            float plen = std::hypot(px, py);
            if (plen <= 1e-5f) {
                px = 1.0f; py = 0.0f; pz = 0.0f; plen = 1.0f;
            }
            px /= plen; py /= plen; pz /= plen;
            const float width = std::min(2.0f, std::max(0.1f, length * 0.08f));
            const float tipF = 0.72f;
            const float baseF = 0.52f;
            const float tip[3] = {segs[i] + dx*tipF, segs[i+1] + dy*tipF,
                                  segs[i+2] + dz*tipF};
            const float base[3] = {segs[i] + dx*baseF, segs[i+1] + dy*baseF,
                                   segs[i+2] + dz*baseF};
            dirs.insert(dirs.end(), tip, tip + 3);
            dirs.push_back(base[0] + px*width); dirs.push_back(base[1] + py*width);
            dirs.push_back(base[2] + pz*width);
            dirs.push_back(base[0] - px*width); dirs.push_back(base[1] - py*width);
            dirs.push_back(base[2] - pz*width);
        }
    };
    buildDirections(bp_.rapid, bp_.rapidDir);
    buildDirections(bp_.cut, bp_.cutDir);
    buildDirections(bp_.lead, bp_.leadDir);
    buildDirections(bp_.plunge, bp_.plungeDir);
    buildDirections(bp_.arc, bp_.arcDir);
}


bool camFieldToMesh(const nlohmann::json& field, Mesh& out) {
    try {
        if (!field.is_object() || !field.contains("nx") || !field.contains("ny") ||
            !field.contains("z") || !field.contains("lo") || !field.contains("res") ||
            !field["nx"].is_number_integer() || !field["ny"].is_number_integer() ||
            !field["res"].is_number()) return false;
        const long long nx64 = field["nx"].get<long long>();
        const long long ny64 = field["ny"].get<long long>();
        const double res = field["res"].get<double>();
        const auto& lo = field["lo"];
        const auto& z = field["z"];
        if (nx64 < 2 || ny64 < 2 || nx64 > 1000000 || ny64 > 1000000 ||
            res <= 0.0 || !std::isfinite(res) || !lo.is_array() || lo.size() < 2 ||
            !lo[0].is_number() || !lo[1].is_number() || !z.is_array()) return false;
        const size_t nxSize = (size_t)nx64;
        const size_t nySize = (size_t)ny64;
        if (nySize > SIZE_MAX / nxSize || z.size() < nxSize * nySize) return false;
        const size_t cellCount = nxSize * nySize;
        for (size_t i = 0; i < cellCount; ++i)
            if (!z[i].is_number()) return false;
        const double lox = lo[0].get<double>();
        const double loy = lo[1].get<double>();
        if (!std::isfinite(lox) || !std::isfinite(loy)) return false;

        const int nx = (int)nx64;
        const int ny = (int)ny64;
        const int step = (cellCount > 250000) ? 2 : 1;
        const int gx = (nx + step - 1) / step;
        const int gy = (ny + step - 1) / step;
        auto zat = [&](int i, int j) -> float {
            i = std::max(0, std::min(nx - 1, i));
            j = std::max(0, std::min(ny - 1, j));
            return z[(size_t)j * nxSize + (size_t)i].get<float>();
        };

        Mesh built;
        built.positions.reserve((size_t)gx * gy * 3);
        built.normals.reserve((size_t)gx * gy * 3);
        built.indices.reserve((size_t)(gx - 1) * (gy - 1) * 6);
        for (int j = 0; j < gy; ++j) {
            for (int i = 0; i < gx; ++i) {
                const int ii = i * step, jj = j * step;
                built.positions.push_back((float)(lox + ii * res));
                built.positions.push_back((float)(loy + jj * res));
                built.positions.push_back(zat(ii, jj));
            }
        }
        for (int j = 0; j < gy; ++j) {
            for (int i = 0; i < gx; ++i) {
                const int ii = i * step, jj = j * step;
                const float zL = zat(ii - step, jj), zR = zat(ii + step, jj);
                const float zD = zat(ii, jj - step), zU = zat(ii, jj + step);
                const float r = (float)(res * step);
                float nxv = (zL - zR) / (2 * r);
                float nyv = (zD - zU) / (2 * r);
                float nzv = 1.0f;
                float len = std::sqrt(nxv * nxv + nyv * nyv + nzv * nzv);
                if (len < 1e-9f) {
                    nxv = 0.0f; nyv = 0.0f; nzv = 1.0f; len = 1.0f;
                }
                built.normals.push_back(nxv / len);
                built.normals.push_back(nyv / len);
                built.normals.push_back(nzv / len);
            }
        }
        for (int j = 0; j < gy - 1; ++j) {
            for (int i = 0; i < gx - 1; ++i) {
                const uint32_t a = (uint32_t)(j * gx + i), b = a + 1;
                const uint32_t c = a + (uint32_t)gx, d = c + 1;
                built.indices.push_back(a); built.indices.push_back(c); built.indices.push_back(b);
                built.indices.push_back(b); built.indices.push_back(c); built.indices.push_back(d);
            }
        }
        out = std::move(built);
        return true;
    } catch (const std::exception&) {
        return false;
    }
}

}  // namespace ui
