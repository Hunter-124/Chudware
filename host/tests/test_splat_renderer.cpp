// RE-28: pure-CPU ctest for the 3DGS splat loader + depth sort (no GL context).
#include "../src/render/splat_renderer.h"

#include <cmath>
#include <cstdio>
#include <string>

static int g_failures = 0;
#define CHECK(cond, msg)                                                    \
    do {                                                                    \
        if (!(cond)) {                                                      \
            std::fprintf(stderr, "FAIL: %s (%s:%d)\n", msg, __FILE__, __LINE__); \
            ++g_failures;                                                   \
        }                                                                   \
    } while (0)

int main(int argc, char** argv) {
    // Fixture path passed by CTest (WORKING_DIRECTORY = build dir); arg[1] = absolute .ply path.
    std::string ply = (argc > 1) ? argv[1] : "host/tests/fixtures/splat_tiny.ply";

    render::SplatCloud cloud;
    std::string err;
    bool ok = render::loadSplatPly(ply, cloud, err);
    CHECK(ok, ("loadSplatPly failed: " + err).c_str());
    CHECK(cloud.size() == 4, "expected 4 gaussians in fixture");

    if (cloud.size() == 4) {
        // Activations applied: opacity = sigmoid(4) ~= 0.982, scale = exp(-2) ~= 0.135.
        const render::Gaussian& g0 = cloud.gaussians[0];
        CHECK(std::fabs(g0.pos.z - 10.0f) < 1e-4f, "gaussian 0 z should be 10");
        CHECK(g0.opacity > 0.9f && g0.opacity < 1.0f, "opacity should be sigmoid-activated");
        CHECK(std::fabs(g0.scale.x - std::exp(-2.0f)) < 1e-4f, "scale should be exp-activated");
        // Quaternion normalized (identity here).
        CHECK(std::fabs(g0.rot[0] - 1.0f) < 1e-4f, "rot w should be ~1");
        // Name-driven extraction proof: the fixture reorders columns + carries extra f_rest_*.
        // g0 has f_dc_0=1.0 (others 0) -> color[0] = 0.5 + C0*1 must be the largest channel; if the
        // loader read by offset instead of name, the reordered layout would scramble this.
        CHECK(std::fabs(g0.color[0] - (0.5f + 0.28209479f)) < 1e-4f,
              "color[0] must come from f_dc_0 by NAME despite reordered/extra columns");
        CHECK(g0.color[0] > g0.color[1] && g0.color[0] > g0.color[2],
              "g0 red channel should dominate (f_dc_0 set, f_dc_1/2 zero)");
        CHECK(std::fabs(g0.normal.z - 1.0f) < 1e-4f, "nz should be extracted by name (=1)");
    }

    // Depth sort: camera at +z looking toward origin. Back-to-front = smallest z first.
    // z values: idx0=10, idx1=-5, idx2=0, idx3=20  ->  order 1,2,0,3.
    Vec3 eye{0.0f, 0.0f, 50.0f};
    Vec3 viewDir{0.0f, 0.0f, -1.0f};
    std::vector<uint32_t> order = render::depthSortIndices(cloud, eye, viewDir);
    CHECK(order.size() == 4, "sort should return 4 indices");
    if (order.size() == 4) {
        CHECK(order[0] == 1, "farthest (z=-5, idx1) should be first");
        CHECK(order[1] == 2, "z=0 (idx2) second");
        CHECK(order[2] == 0, "z=10 (idx0) third");
        CHECK(order[3] == 3, "nearest (z=20, idx3) last");
        // Verify monotonic non-increasing depth (back-to-front).
        float prev = 1e30f;
        for (uint32_t gi : order) {
            float depth = dot(cloud.gaussians[gi].pos - eye, normalize(viewDir));
            CHECK(depth <= prev + 1e-4f, "depth must be non-increasing (back-to-front)");
            prev = depth;
        }
    }

    // Malformed input must fail cleanly (no throw), returning false + a reason.
    render::SplatCloud bad;
    std::string berr;
    CHECK(!render::parseSplatPly("not a ply", 9, bad, berr), "garbage input must fail");
    CHECK(!berr.empty(), "failure must set a reason string");

    if (g_failures == 0) {
        std::printf("test_splat_renderer: all checks passed\n");
        return 0;
    }
    std::fprintf(stderr, "test_splat_renderer: %d failure(s)\n", g_failures);
    return 1;
}
