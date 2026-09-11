// RE-28: pure-CPU ctest for the 3DGS splat loader + depth sort (no GL context).
#include "../src/render/splat_renderer.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <vector>
#include <string>

static int g_failures = 0;
#define CHECK(cond, msg)                                                    \
    do {                                                                    \
        if (!(cond)) {                                                      \
            std::fprintf(stderr, "FAIL: %s (%s:%d)\n", msg, __FILE__, __LINE__); \
            ++g_failures;                                                   \
        }                                                                   \
    } while (0)

static const char* columns[] = {"x","y","z","opacity","scale_0","scale_1","scale_2",
                               "rot_0","rot_1","rot_2","rot_3","f_dc_0","f_dc_1","f_dc_2"};
static std::string header(bool binary, int rest = 0, const std::string& extras = "") {
    std::string text = std::string("ply\nformat ") +
        (binary ? "binary_little_endian" : "ascii") + " 1.0\nelement vertex 1\n";
    for (const char* column : columns) text += std::string("property float ") + column + "\n";
    for (int i = 0; i < rest; ++i) text += "property float f_rest_" + std::to_string(i) + "\n";
    return text + extras + "end_header\n";
}
static const std::vector<float> values = {1,2,3,4,0,0,0,2,0,0,0,0,0,0};
static std::string ascii(const std::vector<float>& row) {
    std::string text;
    for (float value : row) text += std::to_string(value) + " ";
    return text + "\n";
}
static void appendFloat(std::string& text, float value) {
    uint32_t bits; std::memcpy(&bits,&value,sizeof(bits));
    for (int i = 0; i < 4; ++i) text.push_back(char((bits >> (8*i)) & 255));
}
static void rejects(const std::string& text, const char* reason) {
    render::SplatCloud cloud;
    cloud.gaussians.emplace_back(); // failures must not expose partial or stale output
    std::string error;
    CHECK(!render::parseSplatPly(text.data(),text.size(),cloud,error),reason);
    CHECK(cloud.empty() && !error.empty(),"rejected input clears cloud and supplies diagnostic");
}

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
        // Name-driven extraction proof: reordered columns and unknown scalar extras.
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

    // All three variances and the quaternion influence the covariance actually uploaded.
    render::Gaussian anisotropic;
    anisotropic.scale = {3,1,2};
    float covariance[6];
    render::gaussianCovariance(anisotropic,covariance);
    CHECK(std::fabs(covariance[0]-9)<1e-5f && std::fabs(covariance[3]-1)<1e-5f &&
          std::fabs(covariance[5]-4)<1e-5f,"identity covariance preserves three distinct scales");
    anisotropic.rot[0] = std::cos(0.3926990817f);
    anisotropic.rot[3] = std::sin(0.3926990817f); // 45-degree z rotation
    render::gaussianCovariance(anisotropic,covariance);
    CHECK(std::fabs(covariance[0]-5)<1e-4f && std::fabs(covariance[1]-4)<1e-4f &&
          std::fabs(covariance[3]-5)<1e-4f && std::fabs(covariance[5]-4)<1e-4f,
          "quaternion rotates anisotropic covariance and introduces off-diagonal correlation");
    for (float& q : anisotropic.rot) q = -q;
    float same[6]; render::gaussianCovariance(anisotropic,same);
    for (int i=0;i<6;++i) CHECK(std::fabs(same[i]-covariance[i])<1e-5f,
                                "q and -q represent the same Gaussian orientation");
    render::SplatCloud single;
    anisotropic.pos = {10,20,30};
    single.gaussians.push_back(anisotropic);
    render::SplatRenderer renderer;
    Vec3 min,max;
    CHECK(!renderer.bounds(min,max),"empty cloud has no bounds");
    renderer.setCloud(std::move(single));
    CHECK(renderer.bounds(min,max) && std::fabs(min.x-(10-3*std::sqrt(5.0f)))<1e-4f &&
          std::fabs(max.z-36)<1e-4f,"frame-to-fit bounds enclose rotated three-sigma extent");

    std::string valid = header(false) + ascii(values);
    CHECK(render::parseSplatPly(valid.data(),valid.size(),bad,berr),"valid ASCII Gaussian parses");
    rejects(valid.substr(4),"missing magic fails");
    rejects(header(false).substr(0,header(false).size()-11),"missing end_header fails");
    rejects(header(false,0,"property float x\n")+ascii(values),"duplicate property fails");
    rejects(header(false,0,"property list uchar int face\n")+ascii(values),"list property fails");
    rejects(header(false,1)+ascii(values)+"0\n","partial SH band fails");
    rejects(header(false)+ascii(values)+"9\n","trailing scalar fails");
    auto malformed = values; malformed[0] = std::numeric_limits<float>::quiet_NaN();
    rejects(header(false)+ascii(malformed),"nonfinite position fails");
    malformed=values; malformed[4]=1000;
    rejects(header(false)+ascii(malformed),"overflowing activated scale fails");
    malformed=values; malformed[7]=0;
    rejects(header(false)+ascii(malformed),"zero quaternion fails");
    auto huge=valid; huge.replace(huge.find("vertex 1"),8,"vertex 2000001");
    rejects(huge,"oversized vertex count fails before allocation");

    // Unknown integer properties of all widths are consumed correctly, not read as floats.
    std::string binary=header(true,0,
        "property uchar u8\nproperty short i16\nproperty uint u32\nproperty double d64\n");
    for (float v : values) appendFloat(binary,v);
    binary.push_back(char(255));
    binary.push_back(char(254)); binary.push_back(char(255)); // -2
    for (int i=0;i<4;++i) binary.push_back(char(255)); // UINT32_MAX
    const uint64_t doubleBits=0x3ff0000000000000ULL;
    for (int i=0;i<8;++i) binary.push_back(char((doubleBits>>(8*i))&255));
    CHECK(render::parseSplatPly(binary.data(),binary.size(),bad,berr) &&
          bad.size()==1 && bad.gaussians[0].pos.z==3 && bad.gaussians[0].rot[0]==1,
          "mixed-width binary little endian decodes without overread and normalizes quaternion");
    rejects(binary.substr(0,binary.size()-1),"binary truncation fails");
    auto binaryNan=header(true);
    for (size_t i=0;i<values.size();++i)
        appendFloat(binaryNan,i==0?std::numeric_limits<float>::infinity():values[i]);
    rejects(binaryNan,"binary nonfinite data fails");
    for (int rest : {9,24,45}) {
        auto row=values;
        for (int i=0;i<rest;++i) row.push_back(float(i+1));
        auto text=header(false,rest)+ascii(row);
        CHECK(render::parseSplatPly(text.data(),text.size(),bad,berr),"complete SH bands parse");
        if (!bad.empty()) {
            const auto& g=bad.gaussians[0];
            CHECK(g.shDegree==(rest==9?1:rest==24?2:3) &&
                  g.sh[0]==1 && g.sh[15]==float(rest/3+1) &&
                  g.sh[30+rest/3-1]==float(rest),
                  "SH channel-major coefficient layout is preserved across degrees");
        }
    }
    // Rotation in place must change compositing order even when the eye is unchanged.
    auto reverse = render::depthSortIndices(cloud,eye,{0,0,1});
    if (reverse.size()==4) CHECK(reverse[0]==3 && reverse[3]==1,
                               "direction-only camera change reverses back-to-front order");

    if (g_failures == 0) {
        std::printf("test_splat_renderer: all checks passed\n");
        return 0;
    }
    std::fprintf(stderr, "test_splat_renderer: %d failure(s)\n", g_failures);
    return 1;
}
