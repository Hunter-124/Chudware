// RE-21: Canned sparse fixture builder — split out of re_points.cpp so the ctest can
// link it without dragging in the GL-owning RePoints class (and its glad dependency).
// No GL headers included here.
#include "re_points.h"

#include <cmath>

namespace render {

void buildCannedSparseFixture(std::vector<SparsePoint>&   pts,
                               std::vector<CameraFrustum>& frusta) {
    pts.clear();
    frusta.clear();

    // ~200 points on a hemisphere surface — simulates a COLMAP sparse cloud of a scanned object.
    const int N = 200;
    pts.reserve(N);
    for (int i = 0; i < N; ++i) {
        // Golden-angle spiral on unit sphere (upper hemisphere only — face-up object convention).
        const float phi   = std::acos(1.0f - (float)(i + 0.5f) / N);
        const float theta = 2.3999632f * (float)i;  // golden angle radians
        const float sp    = std::sin(phi);
        SparsePoint p;
        p.x = sp * std::cos(theta) * 60.0f;  // ~60 mm radius
        p.y = sp * std::sin(theta) * 60.0f;
        p.z = std::cos(phi) * 40.0f;         // flat-ish; z max ~40 mm
        // Colour: vary from blue-green to white by elevation.
        const float t = (float)i / N;
        p.r = (uint8_t)(80  + (int)(t * 140));
        p.g = (uint8_t)(160 + (int)(t * 70));
        p.b = (uint8_t)(200 + (int)(t * 55));
        pts.push_back(p);
    }

    // 6 camera frusta arranged in a ring at radius 200 mm, tilted toward origin.
    const int NF = 6;
    frusta.reserve(NF);
    for (int i = 0; i < NF; ++i) {
        const float angle = 2.0f * 3.14159265f * (float)i / NF;
        const float R     = 200.0f;
        CameraFrustum f;
        f.cx = R * std::cos(angle);
        f.cy = R * std::sin(angle);
        f.cz = 80.0f;

        // Direction toward origin from camera.
        const float dx = -f.cx, dy = -f.cy, dz = -f.cz;
        const float len = std::sqrt(dx*dx + dy*dy + dz*dz);
        const float nx = dx/len, ny = dy/len, nz = dz/len;

        // Right vector (perpendicular in XY plane).
        const float rx = -std::sin(angle), ry = std::cos(angle), rz = 0.0f;
        // Up = n cross r.
        const float ux = ny*rz - nz*ry, uy = nz*rx - nx*rz, uz = nx*ry - ny*rx;

        const float hw = 15.0f;  // half-width mm
        const float hh = 10.0f;  // half-height mm
        const float fd = 40.0f;  // near-plane distance

        auto corner = [&](float sr, float su, float* ox, float* oy, float* oz) {
            *ox = f.cx + nx*fd + sr*hw*rx + su*hh*ux;
            *oy = f.cy + ny*fd + sr*hw*ry + su*hh*uy;
            *oz = f.cz + nz*fd + sr*hw*rz + su*hh*uz;
        };
        corner(-1,  1, &f.c0x, &f.c0y, &f.c0z);  // TL
        corner( 1,  1, &f.c1x, &f.c1y, &f.c1z);  // TR
        corner( 1, -1, &f.c2x, &f.c2y, &f.c2z);  // BR
        corner(-1, -1, &f.c3x, &f.c3y, &f.c3z);  // BL
        frusta.push_back(f);
    }
}

}  // namespace render
