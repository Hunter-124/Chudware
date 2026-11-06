// Reconstruction-space point picking and span measurement for scale calibration.
#pragma once

#include <array>
#include <cstddef>
#include <cmath>
#include <vector>

#include "vec_math.h"

namespace ui::reveng {

// Project the visible cloud into the preview's top-left-origin screen rectangle.
// Return -1 when the click is not within maxPixels of a visible point.
template<class Point>
int pickSparsePoint(const std::vector<Point>& points, const Mat4& mvp,
                    float left, float top, float width, float height,
                    float mouseX, float mouseY, float maxPixels) {
    if (!(width > 0.0f && height > 0.0f && maxPixels > 0.0f)) return -1;
    float best = maxPixels * maxPixels;
    int picked = -1;
    const float* m = mvp.m;
    for (std::size_t i = 0; i < points.size(); ++i) {
        const auto& p = points[i];
        const float x = m[0]*p.x + m[4]*p.y + m[8]*p.z + m[12];
        const float y = m[1]*p.x + m[5]*p.y + m[9]*p.z + m[13];
        const float z = m[2]*p.x + m[6]*p.y + m[10]*p.z + m[14];
        const float w = m[3]*p.x + m[7]*p.y + m[11]*p.z + m[15];
        if (!(w > 0.0f) || z < -w || z > w) continue;
        const float sx = left + (x / w + 1.0f) * 0.5f * width;
        const float sy = top + (1.0f - y / w) * 0.5f * height;
        const float d = (sx - mouseX)*(sx - mouseX) + (sy - mouseY)*(sy - mouseY);
        if (d < best) { best = d; picked = (int)i; }
    }
    return picked;
}

// For a splat-only source, choose the nearest projected Gaussian center.
// Centers approximate the surface and must not be interpreted as exact CAD edges.
template<class Gaussian>
int pickSplatPoint(const std::vector<Gaussian>& points, const Mat4& mvp,
                   float left, float top, float width, float height,
                   float mouseX, float mouseY, float maxPixels) {
    if (!(width > 0.0f && height > 0.0f && maxPixels > 0.0f)) return -1;
    float best = maxPixels * maxPixels;
    int picked = -1;
    const float* m = mvp.m;
    for (std::size_t i = 0; i < points.size(); ++i) {
        const auto& g = points[i];
        if (g.opacity < 0.1f) continue;
        const auto& p = g.pos;
        const float x = m[0]*p.x + m[4]*p.y + m[8]*p.z + m[12];
        const float y = m[1]*p.x + m[5]*p.y + m[9]*p.z + m[13];
        const float z = m[2]*p.x + m[6]*p.y + m[10]*p.z + m[14];
        const float w = m[3]*p.x + m[7]*p.y + m[11]*p.z + m[15];
        if (!(w > 0.0f) || z < -w || z > w) continue;
        const float sx = left + (x / w + 1.0f) * 0.5f * width;
        const float sy = top + (1.0f - y / w) * 0.5f * height;
        const float d = (sx - mouseX)*(sx - mouseX) + (sy - mouseY)*(sy - mouseY);
        if (d < best) { best = d; picked = (int)i; }
    }
    return picked;
}

// Invert x_mm = scale * R * x_native + translation (R is row-major).
inline std::array<double, 3> inverseSimilarity(const Vec3& p, double scale,
                                                const std::array<double, 9>& r,
                                                const std::array<double, 3>& t) {
    const double v[3] = {(double(p.x) - t[0]) / scale, (double(p.y) - t[1]) / scale,
                         (double(p.z) - t[2]) / scale};
    return {r[0]*v[0] + r[3]*v[1] + r[6]*v[2],
            r[1]*v[0] + r[4]*v[1] + r[7]*v[2],
            r[2]*v[0] + r[5]*v[1] + r[8]*v[2]};
}

inline double reconstructionDistance(const std::array<double, 3>& a,
                                     const std::array<double, 3>& b) {
    const double dx = a[0] - b[0], dy = a[1] - b[1], dz = a[2] - b[2];
    return std::sqrt(dx*dx + dy*dy + dz*dz);
}

inline const char* scaleConfidenceTier(bool measured, double mmPerUnit, bool confirmed,
                                       bool hasResidual, double residualPct) {
    if (!measured || !(mmPerUnit > 0.0) || !std::isfinite(mmPerUnit)) return "approximate";
    if (!confirmed || !hasResidual || !std::isfinite(residualPct) || residualPct < 0.0) return "low";
    if (residualPct < 1.0) return "high";
    if (residualPct < 3.0) return "medium";
    return "low";
}

}  // namespace ui::reveng
