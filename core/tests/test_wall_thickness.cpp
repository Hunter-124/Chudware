// geom/wall_thickness: inscribed-sphere wall thickness on closed box meshes whose answer is
// known exactly. Each case is a way normal rays got printability wrong: edge tapers read
// ~0 at every convex edge, and a thin rib's top face read the rib height.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <cmath>

#include "geom/wall_thickness.h"

using chudware::geom::ThicknessMesh;

namespace {

// A closed axis-aligned box: six faces with their own vertices and outward normals (the
// per-face vertex layout of a B-rep tessellation), one tri_group per face.
ThicknessMesh box(float x0, float y0, float z0, float x1, float y1, float z1) {
    ThicknessMesh m;
    const auto quad = [&](const float c[4][3], const float n[3]) {
        const auto base = static_cast<uint32_t>(m.pos.size() / 3);
        for (int k = 0; k < 4; ++k) {
            m.pos.insert(m.pos.end(), c[k], c[k] + 3);
            m.nrm.insert(m.nrm.end(), n, n + 3);
        }
        const uint32_t tri[6] = {0, 1, 2, 0, 2, 3};
        for (const uint32_t t : tri) m.idx.push_back(base + t);
        const auto g = static_cast<uint32_t>(m.tri_group.empty() ? 0 : m.tri_group.back() + 1);
        m.tri_group.push_back(g);
        m.tri_group.push_back(g);
    };
    // Counter-clockwise seen from outside.
    const float bottom[4][3] = {{x0, y0, z0}, {x0, y1, z0}, {x1, y1, z0}, {x1, y0, z0}};
    const float top[4][3] = {{x0, y0, z1}, {x1, y0, z1}, {x1, y1, z1}, {x0, y1, z1}};
    const float front[4][3] = {{x0, y0, z0}, {x1, y0, z0}, {x1, y0, z1}, {x0, y0, z1}};
    const float back[4][3] = {{x0, y1, z0}, {x0, y1, z1}, {x1, y1, z1}, {x1, y1, z0}};
    const float left[4][3] = {{x0, y0, z0}, {x0, y0, z1}, {x0, y1, z1}, {x0, y1, z0}};
    const float right[4][3] = {{x1, y0, z0}, {x1, y1, z0}, {x1, y1, z1}, {x1, y0, z1}};
    const float nb[3] = {0, 0, -1}, nt[3] = {0, 0, 1}, nf[3] = {0, -1, 0}, nk[3] = {0, 1, 0},
                nl[3] = {-1, 0, 0}, nr[3] = {1, 0, 0};
    quad(bottom, nb);
    quad(top, nt);
    quad(front, nf);
    quad(back, nk);
    quad(left, nl);
    quad(right, nr);
    return m;
}

struct Field {
    ThicknessMesh mesh;
    chudware::geom::ThicknessResult result;
    float lo = 0, hi = 0;
};

// The eval_thickness configuration: refine, then measure every sample against all samples.
Field measure(const ThicknessMesh& base, double spacing, double diag) {
    Field f;
    f.mesh = chudware::geom::refine_mesh(base, spacing);
    chudware::geom::ThicknessOptions opt;
    opt.coverage_radius_mm = std::max(10.0 * spacing, 0.1 * diag);
    opt.coverage_tolerance_mm = 0.1 * spacing;
    f.result = chudware::geom::measure_wall_thickness(f.mesh.pos, f.mesh.nrm, f.mesh.pos,
                                                      f.mesh.nrm, opt);
    const auto& t = f.result.thickness;
    f.lo = *std::min_element(t.begin(), t.end());
    f.hi = *std::max_element(t.begin(), t.end());
    return f;
}

}  // namespace

TEST_CASE("refinement bounds every edge and keeps faces contiguous") {
    const ThicknessMesh m = chudware::geom::refine_mesh(box(0, 0, 0, 40, 30, 2), 0.5);
    for (std::size_t t = 0; t < m.n_tris(); ++t)
        for (int e = 0; e < 3; ++e) {
            const uint32_t a = m.idx[3 * t + e], b = m.idx[3 * t + (e + 1) % 3];
            double d2 = 0;
            for (int k = 0; k < 3; ++k) d2 += std::pow(m.pos[3 * a + k] - m.pos[3 * b + k], 2.0);
            CHECK(std::sqrt(d2) <= 0.5 + 1e-6);
        }
    for (std::size_t t = 1; t < m.n_tris(); ++t) CHECK(m.tri_group[t] >= m.tri_group[t - 1]);
    CHECK(chudware::geom::mesh_area(m) == doctest::Approx(2 * (40 * 30 + 40 * 2 + 30 * 2)));
}

TEST_CASE("a plate reads its thickness everywhere, edges and sides included") {
    // Rays read ~0 at every convex edge and the plate's long span on its side faces.
    const Field f = measure(box(0, 0, 0, 40, 30, 2), 0.5, 50.0);
    CHECK(f.lo == doctest::Approx(2.0).epsilon(5e-4));
    CHECK(f.hi == doctest::Approx(2.0).epsilon(5e-4));
    for (const float c : f.result.opposing_cos) CHECK(c < -0.99f);
}

TEST_CASE("a cube has no thin edges") {
    const Field f = measure(box(0, 0, 0, 20, 20, 20), 1.0, 34.6);
    CHECK(f.lo == doctest::Approx(20.0).epsilon(5e-4));
}

TEST_CASE("a thin rib reads its width on the top and end faces, not its height or length") {
    // 0.8 wide, 20 long, 10 tall: the top face's own ball spans the 10 mm height; the side
    // balls that contain it measure the 0.8 mm the printer actually has to lay down.
    const Field f = measure(box(0, 0, 0, 0.8f, 20, 10), 0.25, 22.4);
    CHECK(f.hi == doctest::Approx(0.8).epsilon(1e-3));
    CHECK(f.lo == doctest::Approx(0.8).epsilon(1e-3));
}

TEST_CASE("thin regions group the thin patch and report its extent") {
    const Field f = measure(box(0, 0, 0, 0.8f, 20, 10), 0.25, 22.4);
    const auto areas = chudware::geom::vertex_areas(f.mesh);
    const auto none = chudware::geom::thin_regions(f.mesh, f.result.thickness, areas, 0.5, 0.375);
    CHECK(none.empty());
    const auto all = chudware::geom::thin_regions(f.mesh, f.result.thickness, areas, 1.0, 0.375);
    REQUIRE(all.size() == 1);
    CHECK(all[0].min_thickness == doctest::Approx(0.8).epsilon(1e-3));
    CHECK(all[0].area == doctest::Approx(2 * (0.8 * 20 + 0.8 * 10 + 20 * 10)).epsilon(1e-6));
    CHECK(all[0].groups.size() == 6);
    CHECK(all[0].hi[2] == doctest::Approx(10.0));
}

TEST_CASE("queries away from the blockers measure the same wall") {
    // The DFAM path: arbitrary samples on the plate measured against its refined surface.
    const ThicknessMesh blockers = chudware::geom::refine_mesh(box(0, 0, 0, 40, 30, 3), 0.5);
    const std::vector<float> pos = {12.3f, 7.7f, 3.0f, 5.1f, 0.0f, 1.2f};
    const std::vector<float> nrm = {0, 0, 1, 0, -1, 0};
    chudware::geom::ThicknessOptions opt;
    opt.coverage_radius_mm = 5.0;
    opt.coverage_tolerance_mm = 0.05;
    const auto r = chudware::geom::measure_wall_thickness(blockers.pos, blockers.nrm, pos, nrm, opt);
    CHECK(r.thickness[0] == doctest::Approx(3.0).epsilon(5e-4));
    // A side-face query reads the long span: coverage runs among queries only.
    CHECK(r.thickness[1] == doctest::Approx(30.0).epsilon(5e-4));
}
