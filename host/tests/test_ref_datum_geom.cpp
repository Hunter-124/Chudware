// Equivalence test for the batched reference-datum overlay geometry (GL-free, no GL context).
//
// The renderer used to upload a 2..6-vertex range and issue one glDrawArrays PER PRIMITIVE every
// frame (per dash, per cone triangle, per octahedron face). render::buildRefDatumBatch now builds
// the whole datum set once into a single vertex array with a merged draw list.
//
// This test states the OLD behaviour as an executable fact: `oldPass` below is the pre-batch draw
// body copied verbatim (uploads + draw calls recorded instead of issued). Both streams are then
// flattened to individual primitives — (mode, color, line width, vertices) in submission order —
// and compared. The datum pass blends with depth-write off, so that stream IS the rendered result:
// if it matches, the pixels match.
#include "render/ref_datum_geom.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <unordered_set>
#include <vector>

static int g_failures = 0;
#define CHECK(cond, msg)                                                         \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::fprintf(stderr, "FAIL: %s (%s:%d)\n", msg, __FILE__, __LINE__); \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

// One rasterized primitive: a line segment (2 verts), a triangle (3 verts) or a line loop (all
// verts of the loop). Line width is 0 for triangles — it cannot affect filled rasterization.
struct Prim {
    unsigned int mode = 0;
    float rgba[4] = {0, 0, 0, 0};
    float lineWidth = 0.0f;
    std::vector<float> verts;
};

static int vertsPerPrim(unsigned int mode, int count) {
    if (mode == render::kModeTriangles) return 3;
    if (mode == render::kModeLines) return 2;
    return count;  // GL_LINE_LOOP: the whole loop is one primitive
}

// ---------------------------------------------------------------------------------------------
// The OLD draw path: verbatim pre-batch body, with `upload`/`glDrawArrays`/`glUniform4f`/
// `glLineWidth` recorded instead of issued. Do NOT "clean this up" — its whole value is being an
// unmodified copy of what shipped before the batch existed.
// ---------------------------------------------------------------------------------------------
struct OldRecorder {
    std::vector<Prim> prims;
    std::vector<float> pending;   // the tiny per-primitive VBO range
    float col[4] = {0, 0, 0, 1};
    float width = 0.0f;
    int uploads = 0;      // glBufferSubData calls
    int drawCalls = 0;    // glDrawArrays calls

    void upload(const float* d, int floats) {
        pending.assign(d, d + floats);
        ++uploads;
    }
    void setCol(float r, float g, float b, float a) {
        col[0] = r;
        col[1] = g;
        col[2] = b;
        col[3] = a;
    }
    void setWidth(float w) { width = w; }
    void draw(unsigned int mode, int count) {
        ++drawCalls;
        const int per = vertsPerPrim(mode, count);
        for (int i = 0; i + per <= count; i += per) {
            Prim p;
            p.mode = mode;
            for (int k = 0; k < 4; ++k) p.rgba[k] = col[k];
            p.lineWidth = (mode == render::kModeTriangles) ? 0.0f : width;
            p.verts.assign(pending.begin() + (size_t)i * 3, pending.begin() + (size_t)(i + per) * 3);
            prims.push_back(p);
        }
    }
};

static void oldPass(const std::vector<render::RefDatum>& datums,
                    const std::unordered_set<std::string>& hidden, const std::string& selectedId,
                    const std::string& editingId, float e, const render::RefDatumPalette& pal,
                    bool axesOnly, OldRecorder& r) {
    const float ps = e * 0.55f;
    const float ms = e * 0.04f;
    const float cs = e * 0.35f;
    auto upload = [&](const float* data, int floats) { r.upload(data, floats); };
    auto setCol = [&](float cr, float cg, float cb, float ca) { r.setCol(cr, cg, cb, ca); };

    for (const auto& d : datums) {
        if (!d.id.empty() && hidden.count(d.id)) continue;
        const bool isAxis = (d.kind == render::RefDatum::Kind::Axis);
        if (axesOnly != isAxis) continue;
        const bool selected = !selectedId.empty() && d.id == selectedId;
        switch (d.kind) {
            case render::RefDatum::Kind::Axis: {
                Vec3 dir = d.dir;
                const float dlen = std::sqrt(dir.x * dir.x + dir.y * dir.y + dir.z * dir.z);
                if (dlen < 1e-8f) break;
                dir = dir * (1.0f / dlen);
                // The OLD half-length rule, inline (the shared helper must still agree with it).
                const float halfLen = (d.id.rfind("axis.auto:", 0) == 0) ? e * 0.55f : e;
                if (halfLen < 1e-8f) break;
                const float* px = pal.axisX;
                const float* py = pal.axisY;
                const float* pz = pal.axisZ;
                const float* pac = pal.accent;
                float ar = pac[0], ag = pac[1], ab = pac[2];
                const float ax = std::fabs(dir.x), ay = std::fabs(dir.y), az = std::fabs(dir.z);
                if (ax >= ay && ax >= az && ax > 0.85f) { ar = px[0]; ag = px[1]; ab = px[2]; }
                else if (ay >= ax && ay >= az && ay > 0.85f) { ar = py[0]; ag = py[1]; ab = py[2]; }
                else if (az >= ax && az >= ay && az > 0.85f) { ar = pz[0]; ag = pz[1]; ab = pz[2]; }
                if (selected) {
                    r.setWidth(4.5f);
                    setCol(pac[0], pac[1], pac[2], 1.0f);
                } else {
                    r.setWidth(2.75f);
                    setCol(ar, ag, ab, 0.92f);
                }
                const float dashLen = halfLen / 20.0f;
                if (dashLen < 1e-9f) break;
                auto drawHalf = [&](const Vec3& hdir) {
                    float s = 0.0f;
                    bool onDash = false;
                    while (s < halfLen - 1e-7f) {
                        const float s1 = std::min(s + dashLen, halfLen);
                        if (onDash && s1 > s) {
                            const Vec3 p0 = d.origin + hdir * s;
                            const Vec3 p1 = d.origin + hdir * s1;
                            float seg[6] = {p0.x, p0.y, p0.z, p1.x, p1.y, p1.z};
                            upload(seg, 6);
                            r.draw(render::kModeLines, 2);
                        }
                        s = s1;
                        onDash = !onDash;
                    }
                };
                drawHalf(dir);
                drawHalf(Vec3(-dir.x, -dir.y, -dir.z));
                break;
            }
            case render::RefDatum::Kind::Plane: {
                static const float uv[4][2] = {{-1, -1}, {1, -1}, {1, 1}, {-1, 1}};
                Vec3 c[4];
                for (int i = 0; i < 4; ++i)
                    c[i] = d.origin + d.xdir * (uv[i][0] * ps) + d.ydir * (uv[i][1] * ps);
                float tri[18] = {
                    c[0].x,c[0].y,c[0].z, c[1].x,c[1].y,c[1].z, c[2].x,c[2].y,c[2].z,
                    c[0].x,c[0].y,c[0].z, c[2].x,c[2].y,c[2].z, c[3].x,c[3].y,c[3].z};
                upload(tri, 18);
                setCol(0.36f, 0.74f, 0.42f, 0.16f);
                r.draw(render::kModeTriangles, 6);
                float loop[12] = {c[0].x,c[0].y,c[0].z, c[1].x,c[1].y,c[1].z,
                                  c[2].x,c[2].y,c[2].z, c[3].x,c[3].y,c[3].z};
                upload(loop, 12);
                if (selected) setCol(0.40f, 0.90f, 0.50f, 1.0f);
                else          setCol(0.30f, 0.62f, 0.34f, 0.85f);
                r.setWidth(selected ? 2.5f : 1.5f);
                r.draw(render::kModeLineLoop, 4);

                if (!editingId.empty() && d.id == editingId) {
                    const float aLen = ps * 0.95f;
                    const float hh   = ps * 0.22f;
                    const float hw   = ps * 0.11f;
                    Vec3 tip  = d.origin + d.dir * aLen;
                    Vec3 base = tip - d.dir * hh;
                    if (selected) setCol(0.45f, 0.95f, 0.55f, 1.0f);
                    else          setCol(0.33f, 0.68f, 0.38f, 0.95f);
                    r.setWidth(selected ? 2.5f : 1.8f);
                    float shaft[6] = {d.origin.x,d.origin.y,d.origin.z, tip.x,tip.y,tip.z};
                    upload(shaft, 6);
                    r.draw(render::kModeLines, 2);
                    Vec3 head[4] = {base + d.xdir * hw, base - d.xdir * hw,
                                    base + d.ydir * hw, base - d.ydir * hw};
                    for (int i = 0; i < 4; ++i) {
                        float seg[6] = {tip.x,tip.y,tip.z, head[i].x,head[i].y,head[i].z};
                        upload(seg, 6);
                        r.draw(render::kModeLines, 2);
                    }
                }
                break;
            }
            case render::RefDatum::Kind::Point: {
                if (d.id == "point.origin") {
                    const float os = e * 0.06f;
                    const float ns = os * 0.80f;
                    static const Vec3 kDirs[3] = {Vec3(1, 0, 0), Vec3(0, 1, 0), Vec3(0, 0, 1)};
                    static const float kAxCol[3][3] = {{0.90f, 0.25f, 0.25f},
                                                       {0.25f, 0.80f, 0.30f},
                                                       {0.30f, 0.45f, 0.95f}};
                    const Vec3 o = d.origin;
                    for (int ai = 0; ai < 3; ++ai) {
                        const Vec3& dir = kDirs[ai];
                        const Vec3& u = kDirs[(ai + 1) % 3];
                        const Vec3& v = kDirs[(ai + 2) % 3];
                        const float rr = kAxCol[ai][0], gg = kAxCol[ai][1], bb = kAxCol[ai][2];
                        const float coneLen = os * 0.34f, coneRad = coneLen * 0.42f;
                        Vec3 tip = o + dir * os;
                        Vec3 cbase = tip - dir * coneLen;
                        setCol(selected ? rr + 0.10f : rr, selected ? gg + 0.10f : gg,
                               selected ? bb + 0.05f : bb, 1.0f);
                        r.setWidth(selected ? 4.5f : 3.5f);
                        float shaft[6] = {o.x, o.y, o.z, cbase.x, cbase.y, cbase.z};
                        upload(shaft, 6);
                        r.draw(render::kModeLines, 2);
                        const int cseg = 12;
                        Vec3 prevR = cbase + u * coneRad;
                        for (int ci = 1; ci <= cseg; ++ci) {
                            float a = (float)ci / cseg * 6.2831853f;
                            Vec3 cur = cbase + (u * std::cos(a) + v * std::sin(a)) * coneRad;
                            float side[9] = {tip.x, tip.y, tip.z, prevR.x, prevR.y, prevR.z,
                                             cur.x, cur.y, cur.z};
                            upload(side, 9);
                            r.draw(render::kModeTriangles, 3);
                            float cap[9] = {cbase.x, cbase.y, cbase.z, cur.x, cur.y, cur.z,
                                            prevR.x, prevR.y, prevR.z};
                            upload(cap, 9);
                            r.draw(render::kModeTriangles, 3);
                            prevR = cur;
                        }
                        setCol(rr, gg, bb, selected ? 0.55f : 0.40f);
                        r.setWidth(2.5f);
                        Vec3 nEnd = o - dir * ns;
                        const int cells = 12;
                        for (int i = 0; i < cells; i += 2) {
                            float t0 = (float)i / cells, t1 = (float)(i + 1) / cells;
                            Vec3 p0 = o + (nEnd - o) * t0, p1 = o + (nEnd - o) * t1;
                            float seg[6] = {p0.x, p0.y, p0.z, p1.x, p1.y, p1.z};
                            upload(seg, 6);
                            r.draw(render::kModeLines, 2);
                        }
                    }
                    {
                        const float rd = os * 0.16f;
                        static const Vec3 A[6] = {Vec3(1, 0, 0), Vec3(-1, 0, 0), Vec3(0, 1, 0),
                                                  Vec3(0, -1, 0), Vec3(0, 0, 1), Vec3(0, 0, -1)};
                        static const int F[8][3] = {{4, 0, 2}, {4, 2, 1}, {4, 1, 3}, {4, 3, 0},
                                                    {5, 2, 0}, {5, 1, 2}, {5, 3, 1}, {5, 0, 3}};
                        auto drawOcta = [&](float rad, float cr, float cg, float cb, float ca) {
                            setCol(cr, cg, cb, ca);
                            for (int f = 0; f < 8; ++f) {
                                Vec3 p0 = o + A[F[f][0]] * rad, p1 = o + A[F[f][1]] * rad,
                                     p2 = o + A[F[f][2]] * rad;
                                float t[9] = {p0.x, p0.y, p0.z, p1.x, p1.y, p1.z, p2.x, p2.y, p2.z};
                                upload(t, 9);
                                r.draw(render::kModeTriangles, 3);
                            }
                        };
                        drawOcta(rd, selected ? 1.0f : 0.92f, selected ? 1.0f : 0.92f,
                                 selected ? 1.0f : 0.95f, 1.0f);
                    }
                    break;
                }
                Vec3 o = d.origin;
                float cross[18] = {
                    o.x - ms,o.y,o.z, o.x + ms,o.y,o.z,
                    o.x,o.y - ms,o.z, o.x,o.y + ms,o.z,
                    o.x,o.y,o.z - ms, o.x,o.y,o.z + ms};
                upload(cross, 18);
                if (selected) setCol(1.0f, 0.95f, 0.45f, 1.0f);
                else          setCol(0.95f, 0.85f, 0.30f, 0.95f);
                r.setWidth(selected ? 3.0f : 2.0f);
                r.draw(render::kModeLines, 6);
                break;
            }
            case render::RefDatum::Kind::Csys: {
                Vec3 o = d.origin, x = o + d.xdir * cs, y = o + d.ydir * cs, z = o + d.dir * cs;
                r.setWidth(2.0f);
                float ax[6] = {o.x,o.y,o.z, x.x,x.y,x.z};
                upload(ax, 6); setCol(0.90f, 0.25f, 0.25f, 0.95f); r.draw(render::kModeLines, 2);
                float ay[6] = {o.x,o.y,o.z, y.x,y.y,y.z};
                upload(ay, 6); setCol(0.25f, 0.80f, 0.30f, 0.95f); r.draw(render::kModeLines, 2);
                float az[6] = {o.x,o.y,o.z, z.x,z.y,z.z};
                upload(az, 6); setCol(0.30f, 0.45f, 0.95f, 0.95f); r.draw(render::kModeLines, 2);
                break;
            }
            case render::RefDatum::Kind::Curve: {
                if (d.points.size() < 2) break;
                r.setWidth(selected ? 3.5f : 2.25f);
                if (selected) setCol(0.35f, 0.95f, 1.0f, 1.0f);
                else          setCol(0.20f, 0.75f, 0.92f, 0.95f);
                for (size_t i = 1; i < d.points.size(); ++i) {
                    const Vec3& p0 = d.points[i - 1];
                    const Vec3& p1 = d.points[i];
                    float seg[6] = {p0.x, p0.y, p0.z, p1.x, p1.y, p1.z};
                    upload(seg, 6);
                    r.draw(render::kModeLines, 2);
                }
                break;
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------

// Expand a batch pass into the same per-primitive stream.
static std::vector<Prim> expand(const render::RefDatumBatch& b,
                                const std::vector<render::RefDatumDraw>& cmds) {
    std::vector<Prim> out;
    for (const auto& c : cmds) {
        const int per = vertsPerPrim(c.mode, c.count);
        for (int i = 0; i + per <= c.count; i += per) {
            Prim p;
            p.mode = c.mode;
            for (int k = 0; k < 4; ++k) p.rgba[k] = c.rgba[k];
            p.lineWidth = c.lineWidth;
            const size_t base = (size_t)(c.first + i) * 3;
            p.verts.assign(b.verts.begin() + base, b.verts.begin() + base + (size_t)per * 3);
            out.push_back(p);
        }
    }
    return out;
}

static bool nearEq(float a, float b) {
    const float scale = std::max(1.0f, std::max(std::fabs(a), std::fabs(b)));
    return std::fabs(a - b) <= 1e-6f * scale;
}

// Compare two primitive streams element by element. Colors and line widths must be EXACT (they are
// literals / palette values, never arithmetic); vertices are compared to a tight relative epsilon
// (the two translation units may contract the identical float expressions differently).
static void comparePass(const char* label, const std::vector<Prim>& oldPrims,
                        const std::vector<Prim>& newPrims) {
    char msg[256];
    std::snprintf(msg, sizeof(msg), "%s: primitive count %zu vs %zu", label, oldPrims.size(),
                  newPrims.size());
    CHECK(oldPrims.size() == newPrims.size(), msg);
    const size_t n = std::min(oldPrims.size(), newPrims.size());
    for (size_t i = 0; i < n; ++i) {
        const Prim& a = oldPrims[i];
        const Prim& b = newPrims[i];
        std::snprintf(msg, sizeof(msg), "%s: primitive %zu mode %u vs %u", label, i, a.mode,
                      b.mode);
        CHECK(a.mode == b.mode, msg);
        std::snprintf(msg, sizeof(msg), "%s: primitive %zu color", label, i);
        CHECK(a.rgba[0] == b.rgba[0] && a.rgba[1] == b.rgba[1] && a.rgba[2] == b.rgba[2] &&
                  a.rgba[3] == b.rgba[3],
              msg);
        std::snprintf(msg, sizeof(msg), "%s: primitive %zu line width %f vs %f", label, i,
                      (double)a.lineWidth, (double)b.lineWidth);
        CHECK(a.lineWidth == b.lineWidth, msg);
        std::snprintf(msg, sizeof(msg), "%s: primitive %zu vertex count", label, i);
        CHECK(a.verts.size() == b.verts.size(), msg);
        const size_t vn = std::min(a.verts.size(), b.verts.size());
        for (size_t k = 0; k < vn; ++k) {
            if (!nearEq(a.verts[k], b.verts[k])) {
                std::snprintf(msg, sizeof(msg), "%s: primitive %zu vertex float %zu: %.9g vs %.9g",
                              label, i, k, (double)a.verts[k], (double)b.verts[k]);
                CHECK(false, msg);
                break;
            }
        }
    }
}

static render::RefDatum axis(const char* id, Vec3 origin, Vec3 dir) {
    render::RefDatum d;
    d.kind = render::RefDatum::Kind::Axis;
    d.id = id;
    d.origin = origin;
    d.dir = dir;
    return d;
}

int main() {
    // A datum set that reaches EVERY branch of the build: cardinal + arbitrary + plane-derived
    // (axis.auto:) axes, a plain plane, the PM-edited plane, the world-origin marker (cone + tail +
    // octahedron), a plain point cross, a coordinate system, a curve, and a hidden datum.
    std::vector<render::RefDatum> datums;
    datums.push_back(axis("axis.x", Vec3(0, 0, 0), Vec3(1, 0, 0)));
    datums.push_back(axis("axis.y", Vec3(1, 2, 3), Vec3(0, 1, 0)));
    datums.push_back(axis("axis.z", Vec3(0, 0, 0), Vec3(0, 0, 2)));      // non-unit dir
    datums.push_back(axis("axis.free", Vec3(-2, 1, 0), Vec3(1, 1, 0)));  // accent fallback
    datums.push_back(axis("axis.auto:p1|p2", Vec3(0, 0, 5), Vec3(0, 1, 0)));
    datums.push_back(axis("axis.degenerate", Vec3(0, 0, 0), Vec3(0, 0, 0)));  // skipped
    datums.push_back(axis("axis.hidden", Vec3(0, 0, 0), Vec3(1, 0, 0)));      // hidden below

    render::RefDatum plane;
    plane.kind = render::RefDatum::Kind::Plane;
    plane.id = "plane.1";
    plane.origin = Vec3(3, -1, 2);
    plane.dir = Vec3(0, 0, 1);
    plane.xdir = Vec3(1, 0, 0);
    plane.ydir = Vec3(0, 1, 0);
    datums.push_back(plane);
    plane.id = "plane.editing";
    plane.origin = Vec3(-4, 2, 1);
    datums.push_back(plane);

    render::RefDatum origin;
    origin.kind = render::RefDatum::Kind::Point;
    origin.id = "point.origin";
    origin.origin = Vec3(0, 0, 0);
    datums.push_back(origin);

    render::RefDatum pt;
    pt.kind = render::RefDatum::Kind::Point;
    pt.id = "point.7";
    pt.origin = Vec3(5, 6, 7);
    datums.push_back(pt);

    render::RefDatum csys;
    csys.kind = render::RefDatum::Kind::Csys;
    csys.id = "csys.1";
    csys.origin = Vec3(1, 1, 1);
    csys.dir = Vec3(0, 0, 1);
    csys.xdir = Vec3(1, 0, 0);
    csys.ydir = Vec3(0, 1, 0);
    datums.push_back(csys);

    render::RefDatum curve;
    curve.kind = render::RefDatum::Kind::Curve;
    curve.id = "curve.helix";
    for (int i = 0; i < 40; ++i) {
        const float t = (float)i * 0.25f;
        curve.points.push_back(Vec3(std::cos(t) * 4.0f, std::sin(t) * 4.0f, t));
    }
    datums.push_back(curve);
    render::RefDatum shortCurve = curve;
    shortCurve.id = "curve.stub";
    shortCurve.points.resize(1);   // < 2 samples: skipped
    datums.push_back(shortCurve);

    const std::unordered_set<std::string> hidden = {"axis.hidden"};
    const render::RefDatumPalette pal;   // defaults mirror ui::palette.axisX/Y/Z + accent
    const float e = 35.0f * 1.25f;       // kEmptySceneRadius * 1.25, the live half-extent

    // Two selection states: nothing selected, and a selected plane + PM-edited plane (the normal
    // arrow branch), so the emphasis colors/widths are covered on both sides.
    struct Case { const char* label; std::string selected; std::string editing; };
    const Case cases[] = {
        {"no selection", "", ""},
        {"plane selected + edited", "plane.editing", "plane.editing"},
        {"axis selected", "axis.x", ""},
        {"origin selected", "point.origin", ""},
    };

    for (const Case& c : cases) {
        render::RefDatumBatch batch;
        render::buildRefDatumBatch(datums, hidden, c.selected, c.editing, e, pal, batch);

        OldRecorder oldNonAxis, oldAxis;
        oldPass(datums, hidden, c.selected, c.editing, e, pal, /*axesOnly=*/false, oldNonAxis);
        oldPass(datums, hidden, c.selected, c.editing, e, pal, /*axesOnly=*/true, oldAxis);

        char label[128];
        std::snprintf(label, sizeof(label), "%s / non-axis pass", c.label);
        comparePass(label, oldNonAxis.prims, expand(batch, batch.nonAxis));
        std::snprintf(label, sizeof(label), "%s / axis pass", c.label);
        comparePass(label, oldAxis.prims, expand(batch, batch.axis));

        // Cost: the old path issued one upload AND one draw per primitive; the batch is one upload
        // plus a handful of draws. Without this the "optimization" could silently be a no-op.
        const int oldDraws = oldNonAxis.drawCalls + oldAxis.drawCalls;
        const int oldUploads = oldNonAxis.uploads + oldAxis.uploads;
        const int newDraws = (int)(batch.nonAxis.size() + batch.axis.size());
        std::snprintf(label, sizeof(label),
                      "%s: batch must cut draw calls hard (old %d draws / %d uploads, new %d draws "
                      "/ 1 upload)",
                      c.label, oldDraws, oldUploads, newDraws);
        CHECK(oldDraws > 150 && newDraws * 8 <= oldDraws, label);
        std::printf("%-26s old %3d draws + %3d uploads -> new %2d draws + 1 upload (%zu verts)\n",
                    c.label, oldDraws, oldUploads, newDraws, batch.verts.size() / 3);
    }

    // The shared axis half-length rule (draw + hit-test single source of truth).
    CHECK(render::refAxisHalfLength("axis.x", e) == e, "plain axis spans the full extent");
    CHECK(render::refAxisHalfLength("axis.auto:a|b", e) == e * 0.55f,
          "plane-derived auto axis spans the user-plane quad");

    // A hidden datum contributes nothing, and clearing the hide brings its geometry back.
    render::RefDatumBatch hiddenBatch, shownBatch;
    render::buildRefDatumBatch(datums, hidden, "", "", e, pal, hiddenBatch);
    render::buildRefDatumBatch(datums, {}, "", "", e, pal, shownBatch);
    CHECK(shownBatch.verts.size() > hiddenBatch.verts.size(),
          "un-hiding axis.hidden must add geometry");

    // Rebuilding into a reused batch must not accumulate (the renderer reuses one instance).
    const size_t verts = shownBatch.verts.size();
    render::buildRefDatumBatch(datums, {}, "", "", e, pal, shownBatch);
    CHECK(shownBatch.verts.size() == verts, "rebuild must replace, not append");

    if (g_failures) {
        std::fprintf(stderr, "%d check(s) failed\n", g_failures);
        return 1;
    }
    std::printf("test_ref_datum_geom: all checks passed\n");
    return 0;
}
