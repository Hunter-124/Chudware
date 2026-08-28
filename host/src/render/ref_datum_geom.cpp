// GL-free reference-datum geometry build (see ref_datum_geom.h). Every literal here — extents,
// dash pitch, cone/octahedron tessellation, colors and line widths — is the geometry the renderer
// drew per-primitive per-frame before it was batched; host/tests/test_ref_datum_geom.cpp replays
// the old per-primitive sequence and asserts the batch expands to the identical stream.
#include "ref_datum_geom.h"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace render {

bool operator==(const RefDatumPalette& a, const RefDatumPalette& b) {
    // All members are float[4] with no padding, so a byte compare is exact and total.
    return std::memcmp(&a, &b, sizeof(RefDatumPalette)) == 0;
}

float refAxisHalfLength(const std::string& id, float extent) {
    // Auto axes from a USER plane pair (axis.auto:<a>|<b>) are drawn only across that plane's
    // quad (half-extent ps = extent*0.55), not the full origin-plane span.
    if (id.rfind("axis.auto:", 0) == 0)
        return extent * 0.55f;
    return extent;
}

namespace {

// Accumulates vertices plus merged draw ranges. Mirrors the GL state machine of the old draw
// path exactly: setCol()/setWidth() are state changes (they emit nothing), add() is a draw call.
// Consecutive draws that share mode + color + line width and are contiguous in the vertex buffer
// collapse into one range — legal because GL_LINES/GL_TRIANGLES primitives are independent and
// are rasterized in submission order either way (the datum pass blends with depth-write off, so
// only ORDER is observable, and order is preserved).
struct Emitter {
    RefDatumBatch& out;
    std::vector<RefDatumDraw>& cmds;
    float col[4] = {0.0f, 0.0f, 0.0f, 1.0f};
    float width = 0.0f;

    void setCol(float r, float g, float b, float a) {
        col[0] = r;
        col[1] = g;
        col[2] = b;
        col[3] = a;
    }
    void setWidth(float w) { width = w; }

    void add(unsigned int mode, const float* xyz, int floats) {
        const int firstVert = (int)(out.verts.size() / 3);
        const int count = floats / 3;
        out.verts.insert(out.verts.end(), xyz, xyz + floats);
        // Line width is meaningless for filled triangles; normalize it to 0 there so triangle
        // ranges merge across the line-width changes the old path interleaved between them.
        const float lw = (mode == kModeTriangles) ? 0.0f : width;
        if (!cmds.empty()) {
            RefDatumDraw& last = cmds.back();
            if (last.mode == mode && mode != kModeLineLoop &&
                last.first + last.count == firstVert && last.lineWidth == lw &&
                last.rgba[0] == col[0] && last.rgba[1] == col[1] && last.rgba[2] == col[2] &&
                last.rgba[3] == col[3]) {
                last.count += count;
                return;
            }
        }
        RefDatumDraw d;
        d.mode = mode;
        d.first = firstVert;
        d.count = count;
        d.lineWidth = lw;
        for (int i = 0; i < 4; ++i) d.rgba[i] = col[i];
        cmds.push_back(d);
    }
};

void buildPass(const std::vector<RefDatum>& datums, const std::unordered_set<std::string>& hidden,
               const std::string& selectedId, const std::string& editingId, float e,
               const RefDatumPalette& pal, bool axesOnly, Emitter& em) {
    // Default-axis half-length equals the origin-plane half-extent e (NOT e*1.6). User-plane auto
    // axes (axis.auto:*) use ps so they span only the drawn user-plane quad. Pickers must call
    // refAxisHalfLength(id) so draw and hit-test share one source of truth.
    const float ps = e * 0.55f;   // half-extent of a datum-plane quad (smaller than origin planes)
    const float ms = e * 0.04f;   // half-size of a point marker cross
    const float cs = e * 0.35f;   // csys axis length

    for (const auto& d : datums) {
        if (!d.id.empty() && hidden.count(d.id)) continue;  // FeatureManager eye hid this datum
        const bool isAxis = (d.kind == RefDatum::Kind::Axis);
        if (axesOnly != isAxis) continue;  // split pass: non-axes then axes (drawn last over planes)
        const bool selected = !selectedId.empty() && d.id == selectedId;
        switch (d.kind) {
            case RefDatum::Kind::Axis: {
                // Dashed reference axis. Drawn AFTER origin planes (axesOnly pass last) so
                // translucent plane fills never wash the line out. CPU-side dashes (core GL has
                // no stipple). World-space pitch is uniform on the whole span so +/- halves are
                // identical (same dash length, count, and spacing) and the pattern clips exactly
                // at ±halfLen — no overshoot past the plane edge. Unselected = per-principal RGB
                // at high opacity; selected = bold full-opacity accent blue.
                Vec3 dir = d.dir;
                const float dlen = std::sqrt(dir.x * dir.x + dir.y * dir.y + dir.z * dir.z);
                if (dlen < 1e-8f) break;
                dir = dir * (1.0f / dlen);
                // Plane-derived auto axes are born from a user plane's quad (half-extent ps);
                // default/user axes span the origin-plane extent e.
                const float halfLen = refAxisHalfLength(d.id, e);
                if (halfLen < 1e-8f) break;
                // Principal-axis tint from palette.axisX/Y/Z. Arbitrary user axes (not near a
                // cardinal) get palette.accent-dim cyan so they still read over planes.
                const float* px = pal.axisX;
                const float* py = pal.axisY;
                const float* pz = pal.axisZ;
                const float* pac = pal.accent;
                float ar = pac[0], ag = pac[1], ab = pac[2];  // accent fallback for free axes
                const float ax = std::fabs(dir.x), ay = std::fabs(dir.y), az = std::fabs(dir.z);
                if (ax >= ay && ax >= az && ax > 0.85f) { ar = px[0]; ag = px[1]; ab = px[2]; }
                else if (ay >= ax && ay >= az && ay > 0.85f) { ar = py[0]; ag = py[1]; ab = py[2]; }
                else if (az >= ax && az >= ay && az > 0.85f) { ar = pz[0]; ag = pz[1]; ab = pz[2]; }
                // Selected: thicker + full-opacity accent (picked reference look).
                // Unselected: axis RGB at high opacity so it stays legible over plane fills.
                if (selected) {
                    em.setWidth(4.5f);
                    em.setCol(pac[0], pac[1], pac[2], 1.0f);
                } else {
                    em.setWidth(2.75f);
                    em.setCol(ar, ag, ab, 0.92f);
                }
                // One world-space pitch for the whole span: dashLen = halfLen/20. Equal dash and
                // gap; walk outward from the origin on BOTH sides so +/- halves are identical
                // (same count, same spacing). Final segment clips exactly at ±halfLen — no
                // overshoot past the plane boundary, no separate tip/arrow past the edge.
                const float dashLen = halfLen / 20.0f;
                if (dashLen < 1e-9f) break;
                auto emitHalf = [&](const Vec3& hdir) {
                    // Start with a gap at the origin so both halves share a continuous centre
                    // gap; the final segment is a dash clipped exactly to halfLen (tip on the
                    // plane edge — full dash when halfLen is an integer multiple of dashLen).
                    float s = 0.0f;
                    bool onDash = false;
                    while (s < halfLen - 1e-7f) {
                        const float s1 = std::min(s + dashLen, halfLen);
                        if (onDash && s1 > s) {
                            const Vec3 p0 = d.origin + hdir * s;
                            const Vec3 p1 = d.origin + hdir * s1;
                            const float seg[6] = {p0.x, p0.y, p0.z, p1.x, p1.y, p1.z};
                            em.add(kModeLines, seg, 6);
                        }
                        s = s1;
                        onDash = !onDash;
                    }
                };
                emitHalf(dir);
                emitHalf(Vec3(-dir.x, -dir.y, -dir.z));
                break;
            }
            case RefDatum::Kind::Plane: {
                static const float uv[4][2] = {{-1, -1}, {1, -1}, {1, 1}, {-1, 1}};
                Vec3 c[4];
                for (int i = 0; i < 4; ++i)
                    c[i] = d.origin + d.xdir * (uv[i][0] * ps) + d.ydir * (uv[i][1] * ps);
                const float tri[18] = {
                    c[0].x,c[0].y,c[0].z, c[1].x,c[1].y,c[1].z, c[2].x,c[2].y,c[2].z,
                    c[0].x,c[0].y,c[0].z, c[2].x,c[2].y,c[2].z, c[3].x,c[3].y,c[3].z};
                em.setCol(0.36f, 0.74f, 0.42f, 0.16f);
                em.add(kModeTriangles, tri, 18);
                const float loop[12] = {c[0].x,c[0].y,c[0].z, c[1].x,c[1].y,c[1].z,
                                        c[2].x,c[2].y,c[2].z, c[3].x,c[3].y,c[3].z};
                if (selected) em.setCol(0.40f, 0.90f, 0.50f, 1.0f);
                else          em.setCol(0.30f, 0.62f, 0.34f, 0.85f);
                em.setWidth(selected ? 2.5f : 1.5f);
                em.add(kModeLineLoop, loop, 12);

                // Normal arrow (SolidWorks plane-normal handle): a shaft from the plane centre along
                // +normal plus a 4-line arrowhead in the in-plane (xdir/ydir) frame, so the user sees
                // which way the new plane faces. Built ONLY while this plane is the one open in the
                // PropertyManager (being created/edited) — once committed the arrow disappears so
                // finished planes don't carry a persistent normal handle.
                if (!editingId.empty() && d.id == editingId) {
                    const float aLen = ps * 0.95f;   // shaft length ~ the quad half-extent
                    const float hh   = ps * 0.22f;   // arrowhead back-set along -normal
                    const float hw   = ps * 0.11f;   // arrowhead half-width
                    Vec3 tip  = d.origin + d.dir * aLen;
                    Vec3 base = tip - d.dir * hh;
                    if (selected) em.setCol(0.45f, 0.95f, 0.55f, 1.0f);
                    else          em.setCol(0.33f, 0.68f, 0.38f, 0.95f);
                    em.setWidth(selected ? 2.5f : 1.8f);
                    const float shaft[6] = {d.origin.x,d.origin.y,d.origin.z, tip.x,tip.y,tip.z};
                    em.add(kModeLines, shaft, 6);
                    Vec3 head[4] = {base + d.xdir * hw, base - d.xdir * hw,
                                    base + d.ydir * hw, base - d.ydir * hw};
                    for (int i = 0; i < 4; ++i) {
                        const float seg[6] = {tip.x,tip.y,tip.z, head[i].x,head[i].y,head[i].z};
                        em.add(kModeLines, seg, 6);
                    }
                }
                break;
            }
            case RefDatum::Kind::Point: {
                // The synthetic WORLD ORIGIN ("point.origin", injected in app.cpp) gets the full
                // CAD-convention treatment instead of the plain cross: color-coded axes (X=red,
                // Y=green, Z=blue), a solid arrow on the positive direction, and a dimmer DASHED
                // tail on the negative direction. No screen-space +/- axis letter labels.
                if (d.id == "point.origin") {
                    const float os = e * 0.06f;   // positive axis length (issue #19: much shorter,
                                                  // arrowheads removed)
                    const float ns = os * 0.80f;  // negative tail length (shorter, subdued)
                    static const Vec3 kDirs[3] = {Vec3(1, 0, 0), Vec3(0, 1, 0), Vec3(0, 0, 1)};
                    static const float kAxCol[3][3] = {{0.90f, 0.25f, 0.25f},   // X red
                                                       {0.25f, 0.80f, 0.30f},   // Y green
                                                       {0.30f, 0.45f, 0.95f}};  // Z blue
                    const Vec3 o = d.origin;
                    for (int ai = 0; ai < 3; ++ai) {
                        const Vec3& dir = kDirs[ai];
                        const Vec3& u = kDirs[(ai + 1) % 3];   // cone base ring lives in the other
                        const Vec3& v = kDirs[(ai + 2) % 3];   // two cardinal axes
                        const float r = kAxCol[ai][0], g = kAxCol[ai][1], b = kAxCol[ai][2];
                        // Positive axis: a thicker colored shaft capped by a REAL 3D cone head
                        // (issue #29). The shaft stops at the cone base so it doesn't poke through.
                        const float coneLen = os * 0.34f, coneRad = coneLen * 0.42f;
                        Vec3 tip = o + dir * os;
                        Vec3 cbase = tip - dir * coneLen;
                        em.setCol(selected ? r + 0.10f : r, selected ? g + 0.10f : g,
                                  selected ? b + 0.05f : b, 1.0f);
                        em.setWidth(selected ? 4.5f : 3.5f);   // ~1px thicker than before
                        const float shaft[6] = {o.x, o.y, o.z, cbase.x, cbase.y, cbase.z};
                        em.add(kModeLines, shaft, 6);
                        // Filled cone: side fan + base cap around the axis, one triangle per ring
                        // step in the same order the per-draw path emitted them. Cull is never
                        // enabled in this renderer, so winding doesn't matter.
                        const int cseg = 12;
                        Vec3 prevR = cbase + u * coneRad;
                        for (int ci = 1; ci <= cseg; ++ci) {
                            float a = (float)ci / cseg * 6.2831853f;
                            Vec3 cur = cbase + (u * std::cos(a) + v * std::sin(a)) * coneRad;
                            const float side[9] = {tip.x, tip.y, tip.z, prevR.x, prevR.y, prevR.z,
                                                   cur.x, cur.y, cur.z};
                            em.add(kModeTriangles, side, 9);
                            const float cap[9] = {cbase.x, cbase.y, cbase.z, cur.x, cur.y, cur.z,
                                                  prevR.x, prevR.y, prevR.z};
                            em.add(kModeTriangles, cap, 9);
                            prevR = cur;
                        }
                        // Negative: dimmer dashed tail (CPU dashes — core GL has no stipple).
                        em.setCol(r, g, b, selected ? 0.55f : 0.40f);
                        em.setWidth(2.5f);
                        Vec3 nEnd = o - dir * ns;
                        const int cells = 12;  // 6 visible dashes
                        for (int i = 0; i < cells; i += 2) {
                            float t0 = (float)i / cells, t1 = (float)(i + 1) / cells;
                            Vec3 p0 = o + (nEnd - o) * t0, p1 = o + (nEnd - o) * t1;
                            const float seg[6] = {p0.x, p0.y, p0.z, p1.x, p1.y, p1.z};
                            em.add(kModeLines, seg, 6);
                        }
                    }
                    // Issue #19/#29: a tiny 3D sphere (octahedron approximation) at absolute (0,0,0)
                    // as a grabbable reference point (selection routes via drawPointPicker for
                    // "point.origin"). Built last, over the axes; the renderer keeps depth-mask off
                    // for the whole pass so the sphere still depth-tests against the solid.
                    {
                        const float rd = os * 0.16f;
                        static const Vec3 A[6] = {Vec3(1, 0, 0), Vec3(-1, 0, 0), Vec3(0, 1, 0),
                                                  Vec3(0, -1, 0), Vec3(0, 0, 1), Vec3(0, 0, -1)};
                        static const int F[8][3] = {{4, 0, 2}, {4, 2, 1}, {4, 1, 3}, {4, 3, 0},
                                                    {5, 2, 0}, {5, 1, 2}, {5, 3, 1}, {5, 0, 3}};
                        // Selected highlight is a constant screen-space ring in viewport_panel
                        // (matches the hover grab radius) — world-space rd*2.4 halo scaled with
                        // scene radius and is intentionally not drawn for point.origin.
                        em.setCol(selected ? 1.0f : 0.92f, selected ? 1.0f : 0.92f,
                                  selected ? 1.0f : 0.95f, 1.0f);
                        for (int f = 0; f < 8; ++f) {
                            Vec3 p0 = o + A[F[f][0]] * rd, p1 = o + A[F[f][1]] * rd,
                                 p2 = o + A[F[f][2]] * rd;
                            const float t[9] = {p0.x, p0.y, p0.z, p1.x, p1.y, p1.z,
                                                p2.x, p2.y, p2.z};
                            em.add(kModeTriangles, t, 9);
                        }
                    }
                    break;
                }
                // A 3-axis cross marker centered on the point.
                Vec3 o = d.origin;
                const float cross[18] = {
                    o.x - ms,o.y,o.z, o.x + ms,o.y,o.z,
                    o.x,o.y - ms,o.z, o.x,o.y + ms,o.z,
                    o.x,o.y,o.z - ms, o.x,o.y,o.z + ms};
                if (selected) em.setCol(1.0f, 0.95f, 0.45f, 1.0f);
                else          em.setCol(0.95f, 0.85f, 0.30f, 0.95f);
                em.setWidth(selected ? 3.0f : 2.0f);
                em.add(kModeLines, cross, 18);
                break;
            }
            case RefDatum::Kind::Csys: {
                // Three axes from the origin: X=xdir (red), Y=ydir (green), Z=normal (blue).
                Vec3 o = d.origin, x = o + d.xdir * cs, y = o + d.ydir * cs, z = o + d.dir * cs;
                em.setWidth(2.0f);
                const float axv[6] = {o.x,o.y,o.z, x.x,x.y,x.z};
                em.setCol(0.90f, 0.25f, 0.25f, 0.95f); em.add(kModeLines, axv, 6);
                const float ayv[6] = {o.x,o.y,o.z, y.x,y.y,y.z};
                em.setCol(0.25f, 0.80f, 0.30f, 0.95f); em.add(kModeLines, ayv, 6);
                const float azv[6] = {o.x,o.y,o.z, z.x,z.y,z.z};
                em.setCol(0.30f, 0.45f, 0.95f, 0.95f); em.add(kModeLines, azv, 6);
                break;
            }
            case RefDatum::Kind::Curve: {
                // A helix / spiral / spline path: the sampled polyline as a solid cyan curve
                // (brighter + thicker when selected). One GL_LINES segment per sample pair, all
                // merged into a single range by the emitter.
                if (d.points.size() < 2) break;
                em.setWidth(selected ? 3.5f : 2.25f);
                if (selected) em.setCol(0.35f, 0.95f, 1.0f, 1.0f);
                else          em.setCol(0.20f, 0.75f, 0.92f, 0.95f);
                for (size_t i = 1; i < d.points.size(); ++i) {
                    const Vec3& p0 = d.points[i - 1];
                    const Vec3& p1 = d.points[i];
                    const float seg[6] = {p0.x, p0.y, p0.z, p1.x, p1.y, p1.z};
                    em.add(kModeLines, seg, 6);
                }
                break;
            }
        }
    }
}

}  // namespace

void buildRefDatumBatch(const std::vector<RefDatum>& datums,
                        const std::unordered_set<std::string>& hidden,
                        const std::string& selectedId, const std::string& editingId, float extent,
                        const RefDatumPalette& pal, RefDatumBatch& out) {
    out.clear();
    // Non-axis datums first, then axes LAST so dashed axes composite OVER the translucent plane
    // fills (the pass writes no depth, so submission order alone decides compositing).
    Emitter nonAxis{out, out.nonAxis};
    buildPass(datums, hidden, selectedId, editingId, extent, pal, /*axesOnly=*/false, nonAxis);
    Emitter axis{out, out.axis};
    buildPass(datums, hidden, selectedId, editingId, extent, pal, /*axesOnly=*/true, axis);
}

}  // namespace render
