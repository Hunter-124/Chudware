// gen_sim2_result_fixture — regenerate the committed result-contour AICM fixture.
//
// The CHUDWARE_SHOT=fea_overlay headless scenario renders a color-mapped FEA
// contour from a precomputed result AICM. This generator uses a deterministic
// structured cantilever mesh, the real solver, and the production
// surface-extraction/overlay pipeline.
//
// Usage (default writes next to the repo fixture):
//   gen_sim2_result_fixture [out.aicm]
// Regenerate the committed fixture from the repo root after a build:
//   ./core/build-linux/bin/gen_sim2_result_fixture contracts/fixtures/sim2_cantilever_result.aicm
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "sim/fea_static.h"
#include "sim/overlay.h"

namespace {
struct BoxMesh { std::vector<double> points; std::vector<int> tets; };

BoxMesh make_box_tet4(int nx, int ny, int nz, double Lx, double Ly, double Lz) {
    BoxMesh m;
    const int NX = nx + 1, NY = ny + 1, NZ = nz + 1;
    auto gid = [&](int i, int j, int k) { return (k * NY + j) * NX + i; };
    m.points.resize(static_cast<size_t>(NX) * NY * NZ * 3);
    for (int k = 0; k < NZ; ++k)
        for (int j = 0; j < NY; ++j)
            for (int i = 0; i < NX; ++i) {
                const int id = gid(i, j, k);
                m.points[id * 3 + 0] = Lx * i / nx;
                m.points[id * 3 + 1] = Ly * j / ny;
                m.points[id * 3 + 2] = Lz * k / nz;
            }
    static const int TET[6][4] = {{0,1,2,6},{0,2,3,6},{0,3,7,6},{0,7,4,6},{0,4,5,6},{0,5,1,6}};
    for (int k = 0; k < nz; ++k)
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                const int c[8] = {gid(i,j,k), gid(i+1,j,k), gid(i+1,j+1,k), gid(i,j+1,k),
                                  gid(i,j,k+1), gid(i+1,j,k+1), gid(i+1,j+1,k+1), gid(i,j+1,k+1)};
                for (auto& tt : TET) for (int v = 0; v < 4; ++v) m.tets.push_back(c[tt[v]]);
            }
    return m;
}
}  // namespace

int main(int argc, char** argv) {
    const std::string outPath = (argc > 1) ? argv[1] : "sim2_cantilever_result.aicm";
    // Slender steel cantilever: 120 x 20 x 12 mm, fixed at x=0, 400 N downward over the x=Lx tip.
    // The classic "bracket under load" bending case: a von-Mises gradient peaking at the root.
    const double Lx = 120.0, Ly = 20.0, Lz = 12.0;
    BoxMesh m = make_box_tet4(24, 4, 3, Lx, Ly, Lz);
    const size_t n = m.points.size() / 3;

    std::vector<int> fixed, tipNodes;
    std::vector<double> forces(n * 3, 0.0);
    for (size_t i = 0; i < n; ++i) {
        const double x = m.points[i * 3 + 0];
        if (x < 1e-6) fixed.push_back(static_cast<int>(i));
        if (std::abs(x - Lx) < 1e-6) tipNodes.push_back(static_cast<int>(i));
    }
    const double totalN = -400.0;
    for (int id : tipNodes) forces[id * 3 + 2] = totalN / static_cast<double>(tipNodes.size());

    auto res = chudware::sim::fea_static_solve(m.points, m.tets, 200000.0, 0.29, 250.0,
                                            fixed, forces, /*nodes_per_elem=*/4);
    std::printf("solve ok=%d n_nodes=%d n_elems=%d max_vm=%.2f MPa max_disp=%.4f mm min_fos=%.2f\n",
                (int)res.ok, res.n_nodes, res.n_elems, res.max_von_mises_mpa, res.max_disp_mm,
                res.min_fos);
    if (!res.ok) { std::printf("solve FAILED: %s\n", res.error.c_str()); return 1; }

    nlohmann::json summary = {{"max_von_mises_mpa", res.max_von_mises_mpa},
                              {"max_disp_mm", res.max_disp_mm}, {"min_fos", res.min_fos}};
    const std::string aicm = chudware::sim::static_overlay(
        /*step_path=*/"", m.points, m.tets, /*nodes_per_elem=*/4,
        res.von_mises, res.disp_magnitude, res.displacement,
        res.principal1, res.principal3, res.max_shear, 250.0, summary);
    if (aicm.empty()) { std::printf("static_overlay returned EMPTY (cap hit or write failure)\n"); return 2; }

    std::FILE* in = std::fopen(aicm.c_str(), "rb");
    std::FILE* out = std::fopen(outPath.c_str(), "wb");
    if (!in || !out) { std::printf("copy to %s failed\n", outPath.c_str()); return 3; }
    std::vector<char> buf(1 << 16); size_t r;
    while ((r = std::fread(buf.data(), 1, buf.size(), in)) > 0) std::fwrite(buf.data(), 1, r, out);
    std::fclose(in); std::fclose(out);
    std::printf("wrote result-contour AICM fixture: %s\n", outPath.c_str());
    return 0;
}
