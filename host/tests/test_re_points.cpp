// RE-21: Pure-CPU unit test for re_points fixture geometry (no GL context required).
// Links only re_points_fixture.cpp — no glad/GLFW. Validates buildCannedSparseFixture:
// point count, bounds, frustum count, frustum near-plane geometry.
#include <cassert>
#include <cmath>
#include <cstdio>

#include "render/re_points.h"

int main() {
    std::vector<render::SparsePoint>   pts;
    std::vector<render::CameraFrustum> frusta;
    render::buildCannedSparseFixture(pts, frusta);

    // --- point cloud ---
    assert(pts.size() == 200 && "expected exactly 200 sparse points");

    int nonZero = 0;
    for (const auto& p : pts) {
        float r2 = p.x*p.x + p.y*p.y + p.z*p.z;
        if (r2 > 1.0f) ++nonZero;
    }
    assert(nonZero == 200 && "all points should be non-zero (on hemisphere surface)");

    for (const auto& p : pts) {
        assert(std::abs(p.x) < 80.0f);
        assert(std::abs(p.y) < 80.0f);
        assert(p.z > -5.0f && p.z < 50.0f);
    }

    // --- frusta ---
    assert(frusta.size() == 6 && "expected exactly 6 camera frusta");
    for (const auto& f : frusta) {
        const float r = std::sqrt(f.cx*f.cx + f.cy*f.cy);
        assert(r > 180.0f && r < 220.0f && "camera centre not at expected ring radius");

        const float d0 = std::sqrt((f.c0x-f.cx)*(f.c0x-f.cx) +
                                   (f.c0y-f.cy)*(f.c0y-f.cy) +
                                   (f.c0z-f.cz)*(f.c0z-f.cz));
        assert(d0 > 5.0f && "corner 0 too close to camera centre — near plane collapsed");

        const float d01 = std::sqrt((f.c0x-f.c1x)*(f.c0x-f.c1x) +
                                    (f.c0y-f.c1y)*(f.c0y-f.c1y) +
                                    (f.c0z-f.c1z)*(f.c0z-f.c1z));
        assert(d01 > 1.0f && "corners 0 and 1 collapsed");
    }

    std::printf("test_re_points PASS: %zu points, %zu frusta — fixture geometry valid.\n",
                pts.size(), frusta.size());
    return 0;
}
