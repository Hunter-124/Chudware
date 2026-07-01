#include "sketch_internal.h"
#include "sketch_mode.h"
#include "sketch_seam.h"

#include <cctype>
#include <cstdio>

namespace ui::sketch {

static SketchPalette gPal;

SketchPalette& sketchPalette() { return gPal; }

void parseEntities(const nlohmann::json& res, std::vector<ui::SketchEntity>& out) {
    out.clear();
    const nlohmann::json* stats = res.contains("statuses") ? &res["statuses"] : nullptr;
    if (!res.contains("entities") || !res["entities"].is_array()) return;
    for (const auto& e : res["entities"]) {
        ui::SketchEntity se;
        se.id = e.value("id", std::string());
        se.type = e.value("type", std::string());
        se.construction = e.value("construction", false);
        if (stats && stats->contains(se.id)) se.status = (*stats)[se.id].get<std::string>();
        else se.status = "under";
        auto v2 = [&](const char* k, float o[2]) {
            if (e.contains(k) && e[k].is_array() && e[k].size() == 2) {
                o[0] = e[k][0].get<float>(); o[1] = e[k][1].get<float>();
            }
        };
        v2("p1", se.p1); v2("p2", se.p2); v2("center", se.center);
        v2("p", se.p1);
        se.radius = e.value("radius", 0.0f);
        se.start = e.value("start", 0.0f);
        se.end = e.value("end", 0.0f);
        se.ccw = e.value("ccw", true);
        se.width = e.value("width", 0.0f);
        se.rx = e.value("rx", 0.0f);
        se.ry = e.value("ry", 0.0f);
        se.rot = e.value("rot", 0.0f);
        se.pts.clear();
        if (e.contains("points") && e["points"].is_array())
            for (const auto& q : e["points"])
                if (q.is_array() && q.size() == 2)
                    se.pts.push_back(ImVec2(q[0].get<float>(), q[1].get<float>()));
        if (se.type == "arc_slot") {
            se.start = e.value("a0", 0.0f);
            se.end = e.value("a1", 0.0f);
            se.ccw = true;
        }
        out.push_back(se);
    }
}

std::string skBaseId(const std::string& handle) {
    return handle.substr(0, handle.find('.'));
}

const nlohmann::json* sketchParamsInTree(SketchHost& st, const std::string& fid) {
    const nlohmann::json& tree = st.docMirror().tree();
    if (!tree.is_object()) return nullptr;
    auto fa = tree.find("features");
    if (fa == tree.end() || !fa->is_array()) return nullptr;
    for (const auto& f : *fa) {
        if (!f.is_object()) continue;
        if (f.value("id", std::string()) != fid) continue;
        auto p = f.find("params");
        return (p != f.end() && p->is_object()) ? &(*p) : nullptr;
    }
    return nullptr;
}

void resyncMirror(SketchHost& st) {
    st.docMirror().resync(st.worker());
}

bool pointInPolyUV(const std::vector<ImVec2>& poly, float x, float y) {
    bool in = false;
    size_t n = poly.size();
    if (n < 3) return false;
    for (size_t i = 0, j = n - 1; i < n; j = i++) {
        bool cross = (poly[i].y > y) != (poly[j].y > y);
        if (cross && x < (poly[j].x - poly[i].x) * (y - poly[i].y) /
                             (poly[j].y - poly[i].y) + poly[i].x)
            in = !in;
    }
    return in;
}

float polyAreaUV(const std::vector<ImVec2>& p) {
    float a = 0.0f;
    for (size_t i = 0, j = p.size() - 1; i < p.size(); j = i++)
        a += (p[j].x + p[i].x) * (p[j].y - p[i].y);
    return std::fabs(a) * 0.5f;
}

bool ptInRect(const ImVec2& p, const ImVec2& lo, const ImVec2& hi) {
    return p.x >= lo.x && p.x <= hi.x && p.y >= lo.y && p.y <= hi.y;
}

static bool segSegCross(const ImVec2& p1, const ImVec2& p2, const ImVec2& p3, const ImVec2& p4) {
    auto cr = [](const ImVec2& a, const ImVec2& b, const ImVec2& c) {
        return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
    };
    float d1 = cr(p3, p4, p1), d2 = cr(p3, p4, p2);
    float d3 = cr(p1, p2, p3), d4 = cr(p1, p2, p4);
    return ((d1 > 0) != (d2 > 0)) && ((d3 > 0) != (d4 > 0));
}

bool segCrossesRect(const ImVec2& a, const ImVec2& b, const ImVec2& lo, const ImVec2& hi) {
    ImVec2 c00(lo.x, lo.y), c10(hi.x, lo.y), c11(hi.x, hi.y), c01(lo.x, hi.y);
    return segSegCross(a, b, c00, c10) || segSegCross(a, b, c10, c11) ||
           segSegCross(a, b, c11, c01) || segSegCross(a, b, c01, c00);
}

std::vector<ImVec2> catmullRomUV(const std::vector<ImVec2>& p, int perSeg) {
    if (p.size() < 3) return p;
    std::vector<ImVec2> out;
    out.reserve((p.size() - 1) * (size_t)perSeg + 1);
    auto at = [&](int i) -> ImVec2 {
        return p[(size_t)std::min(std::max(i, 0), (int)p.size() - 1)];
    };
    for (size_t i = 0; i + 1 < p.size(); ++i) {
        ImVec2 p0 = at((int)i - 1), p1 = p[i], p2 = p[i + 1], p3 = at((int)i + 2);
        for (int s = 0; s < perSeg; ++s) {
            float t = s / (float)perSeg, t2 = t * t, t3 = t2 * t;
            float bx = 0.5f * ((2 * p1.x) + (-p0.x + p2.x) * t +
                               (2 * p0.x - 5 * p1.x + 4 * p2.x - p3.x) * t2 +
                               (-p0.x + 3 * p1.x - 3 * p2.x + p3.x) * t3);
            float by = 0.5f * ((2 * p1.y) + (-p0.y + p2.y) * t +
                               (2 * p0.y - 5 * p1.y + 4 * p2.y - p3.y) * t2 +
                               (-p0.y + 3 * p1.y - 3 * p2.y + p3.y) * t3);
            out.push_back(ImVec2(bx, by));
        }
    }
    out.push_back(p.back());
    return out;
}

bool circumcircle(const ImVec2& a, const ImVec2& b, const ImVec2& c,
                  float& cu, float& cv, float& r) {
    float d = 2.0f * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y));
    if (std::fabs(d) < 1e-6f) return false;
    float a2 = a.x * a.x + a.y * a.y, b2 = b.x * b.x + b.y * b.y, c2 = c.x * c.x + c.y * c.y;
    cu = (a2 * (b.y - c.y) + b2 * (c.y - a.y) + c2 * (a.y - b.y)) / d;
    cv = (a2 * (c.x - b.x) + b2 * (a.x - c.x) + c2 * (b.x - a.x)) / d;
    r = std::hypot(a.x - cu, a.y - cv);
    return true;
}

void drawDashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col_,
                    float thickness, float dash, float gap) {
    float dx = b.x - a.x, dy = b.y - a.y;
    float L = std::hypot(dx, dy);
    if (L < 1.0f) return;
    float step = dash + gap;
    for (float t = 0.0f; t < L; t += step) {
        float e = std::fmin(t + dash, L);
        dl->AddLine(ImVec2(a.x + dx * (t / L), a.y + dy * (t / L)),
                    ImVec2(a.x + dx * (e / L), a.y + dy * (e / L)), col_, thickness);
    }
}

void drawDashedPolyline(ImDrawList* dl, const std::vector<ImVec2>& pts, ImU32 col_,
                        float thickness, float dash, float gap) {
    if (pts.size() < 2) return;
    float phase = 0.0f;
    bool on = true;
    for (size_t i = 1; i < pts.size(); ++i) {
        ImVec2 a = pts[i - 1], b = pts[i];
        float dx = b.x - a.x, dy = b.y - a.y;
        float segLen = std::hypot(dx, dy);
        if (segLen < 1e-4f) continue;
        float walked = 0.0f;
        while (walked < segLen) {
            float span = on ? dash : gap;
            float rem = span - phase;
            float avail = segLen - walked;
            float step = std::fmin(rem, avail);
            if (on) {
                float f0 = walked / segLen, f1 = (walked + step) / segLen;
                dl->AddLine(ImVec2(a.x + dx * f0, a.y + dy * f0),
                            ImVec2(a.x + dx * f1, a.y + dy * f1), col_, thickness);
            }
            walked += step;
            phase += step;
            if (phase >= span - 1e-4f) { on = !on; phase = 0.0f; }
        }
    }
}

bool parseSketchDim(const std::string& text, double& outMm, std::string& err, bool angle,
                    double bareLengthScale) {
    err.clear();
    std::string s;
    for (char c : text) if (!std::isspace((unsigned char)c)) s += c;
    if (s.empty()) { err = "empty"; return false; }
    for (auto& c : s) c = (char)std::tolower((unsigned char)c);
    if (angle) {
        if (!s.empty() && s.back() == 'g' && s.size() >= 3 && s[s.size() - 3] == 'd' &&
            s[s.size() - 2] == 'e')
            s.resize(s.size() - 3);
        char* end = nullptr;
        double v = std::strtod(s.c_str(), &end);
        if (end == s.c_str() || (end && *end)) { err = "bad value"; return false; }
        outMm = v;
        return true;
    }
    double scale = bareLengthScale;
    if (s.size() >= 2 && s.compare(s.size() - 2, 2, "mm") == 0) {
        s.resize(s.size() - 2);
        scale = 1.0;
    } else if (s.size() >= 2 && s.compare(s.size() - 2, 2, "in") == 0) {
        s.resize(s.size() - 2);
        scale = 25.4;
    }
    char* end = nullptr;
    double v = std::strtod(s.c_str(), &end);
    if (end == s.c_str() || (end && *end)) { err = "bad value"; return false; }
    outMm = v * scale;
    return true;
}

std::string formatSketchDim(double mm, bool ips, bool angle) {
    char buf[48];
    if (angle) std::snprintf(buf, sizeof(buf), "%.4gdeg", mm);
    else if (ips) std::snprintf(buf, sizeof(buf), "%.4gin", mm / 25.4);
    else std::snprintf(buf, sizeof(buf), "%.4gmm", mm);
    return buf;
}

bool arcSweepContains(const ui::SketchEntity& arc, float ang) {
    float a0 = std::fmod(arc.start, 360.0f); if (a0 < 0) a0 += 360.0f;
    float a1 = std::fmod(arc.end, 360.0f);   if (a1 < 0) a1 += 360.0f;
    float a = std::fmod(ang, 360.0f);        if (a < 0)  a += 360.0f;
    float span, rel;
    if (arc.ccw) { span = std::fmod(a1 - a0 + 360.0f, 360.0f); rel = std::fmod(a - a0 + 360.0f, 360.0f); }
    else         { span = std::fmod(a0 - a1 + 360.0f, 360.0f); rel = std::fmod(a0 - a + 360.0f, 360.0f); }
    if (span < 1e-7f) span = 360.0f;
    return rel <= span + 1e-5f;
}

bool g_skIps = false;

const char* skLenUnit() { return g_skIps ? "in" : "mm"; }

std::string fmtLen(double mm) {
    double v = g_skIps ? mm / 25.4 : mm;
    char b[48];
    std::snprintf(b, sizeof(b), g_skIps ? "%.4g" : "%.2f", v);
    return b;
}

std::string fmtAng(double deg) {
    char b[48];
    std::snprintf(b, sizeof(b), "%.2f", deg);
    return std::string(b) + "\xC2\xB0";
}

void arrowHead(ImDrawList* dl, ImVec2 tip, float dx, float dy, float sz, ImU32 c) {
    float len = std::hypot(dx, dy);
    if (len < 1e-6f) return;
    dx /= len; dy /= len;
    ImVec2 p1(tip.x - dx * sz - dy * sz * 0.4f, tip.y - dy * sz + dx * sz * 0.4f);
    ImVec2 p2(tip.x - dx * sz + dy * sz * 0.4f, tip.y - dy * sz - dx * sz * 0.4f);
    dl->AddTriangleFilled(tip, p1, p2, c);
}

}  // namespace ui::sketch
