// test_netgen_mesh — end-to-end check of the Netgen/nglib tet mesher (sim/mesher.cpp) and
// the FEA static glue on a REAL OCCT solid (a cantilever beam written to STEP).
//
// Validates the gmsh->Netgen swap with NO regression of capability:
//   • mesh_step produces tet4 AND tet10 meshes with sane quality metrics,
//   • a finer setting yields more elements (curvature/size band responds),
//   • a cantilever static solve CONVERGES TOWARD the Euler-Bernoulli analytic tip
//     deflection, and the FINE mesh is closer than the COARSE one (Bug F7 guard:
//     refinement must not drift away from the reference).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <filesystem>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "sim/mesher.h"
#include "sim/fea_run.h"

#include <BRepPrimAPI_MakeBox.hxx>
#include <STEPControl_Writer.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>

using namespace chudware::sim;
using nlohmann::json;

namespace {
// Cantilever geometry (mm): along X, square WxH cross-section.
constexpr double L = 100.0, W = 10.0, H = 10.0;

std::string write_beam_step() {
    TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), L, W, H).Shape();
    auto p = std::filesystem::temp_directory_path() / "chudware_netgen_beam.step";
    STEPControl_Writer wr;
    REQUIRE(wr.Transfer(box, STEPControl_AsIs) == IFSelect_RetDone);
    REQUIRE(wr.Write(p.string().c_str()) == IFSelect_RetDone);
    return p.string();
}

// A grid of sample points covering the x=x0 end face (y in [0,W], z in [0,H]).
json face_samples(double x0) {
    json pts = json::array();
    for (int i = 0; i <= 4; ++i)
        for (int j = 0; j <= 4; ++j)
            pts.push_back({x0, W * i / 4.0, H * j / 4.0});
    return pts;
}

// Build a cantilever static study: fixed at x=0, transverse -Z point load F at x=L.
json beam_study(const std::string& step, double fineness, int order, double F) {
    json s;
    s["step_path"] = step;
    s["fineness"] = fineness;
    s["mesh_order"] = order;
    s["material"] = {{"E_mpa", 200000.0}, {"nu", 0.29}, {"yield_mpa", 250.0},
                     {"density_g_cm3", 7.85}};
    // Tight face tolerance so ONLY the true end-face nodes (exactly on x=x0) are caught,
    // independent of mesh fineness — otherwise a finer mesh clamps/loads a thicker slab and
    // the convergence study is dominated by that artifact. fea_run_static applies the load
    // `vector` as the TOTAL force (magnitude is ignored for type=="force"), so the full force
    // goes in the vector.
    s["fixtures"] = json::array({json{{"points", face_samples(0.0)}, {"tol_mm", 0.5}}});
    s["loads"] = json::array({json{{"faces", {{"points", face_samples(L)}, {"tol_mm", 0.5}}},
                                    {"type", "force"},
                                    {"vector", {0.0, 0.0, -F}}}});
    return s;
}
}  // namespace

TEST_CASE("netgen mesher produces tet4 + tet10 with sane quality") {
    const std::string step = write_beam_step();

    MeshOptions o;
    o.fineness = 0.4;
    o.order = 1;
    MeshOut t4 = mesh_step(step, o);
    REQUIRE_MESSAGE(t4.ok, t4.error);
    CHECK(t4.nodes_per_elem == 4);
    CHECK(t4.n_elems > 0);
    CHECK(t4.n_nodes > 0);
    CHECK(t4.tets.size() == static_cast<size_t>(t4.n_elems) * 4);
    // Quality: positive worst Jacobian (no inverted tets), finite aspect, few poor elems.
    CHECK(t4.jacobian_min > 0.0);
    CHECK(std::isfinite(t4.aspect_max));
    CHECK(t4.aspect_avg >= 1.0);
    CHECK(t4.pct_poor < 25.0);
    CHECK(t4.min_tet_volume_mm3 > 0.0);

    o.order = 2;
    MeshOut t10 = mesh_step(step, o);
    REQUIRE_MESSAGE(t10.ok, t10.error);
    CHECK(t10.nodes_per_elem == 10);
    CHECK(t10.tets.size() == static_cast<size_t>(t10.n_elems) * 10);
    CHECK(t10.jacobian_min > 0.0);

    // A finer setting must yield more elements (size band / curvature respond).
    o.order = 1;
    o.fineness = 0.8;
    MeshOut fine = mesh_step(step, o);
    REQUIRE_MESSAGE(fine.ok, fine.error);
    CHECK(fine.n_elems > t4.n_elems);
}

TEST_CASE("cantilever static converges toward the Euler-Bernoulli deflection (Bug F7)") {
    const std::string step = write_beam_step();
    const double F = 100.0;                              // N
    const double I = W * H * H * H / 12.0;               // mm^4
    const double E = 200000.0;                           // MPa = N/mm^2
    const double delta_eb = F * L * L * L / (3.0 * E * I);  // Euler-Bernoulli tip deflection (mm)

    // tet10 (accurate) at coarse and fine.
    json coarse = fea_run_static(beam_study(step, 0.35, 2, F));
    json fine = fea_run_static(beam_study(step, 0.75, 2, F));
    REQUIRE_MESSAGE(coarse.value("ok", false), coarse.dump());
    REQUIRE_MESSAGE(fine.value("ok", false), fine.dump());

    const double d_coarse = coarse["summary"]["max_disp_mm"].get<double>();
    const double d_fine = fine["summary"]["max_disp_mm"].get<double>();
    MESSAGE("analytic=" << delta_eb << " coarse=" << d_coarse << " fine=" << d_fine);

    // Both under-predict (FEA is stiffer than the beam theory ignores shear), but must be in
    // the right ballpark and the FINE mesh must be CLOSER to analytic than the coarse one.
    CHECK(d_coarse > 0.0);
    CHECK(d_fine > 0.0);
    CHECK(d_fine <= delta_eb * 1.15);                    // not wildly over
    CHECK(std::abs(d_fine - delta_eb) <= std::abs(d_coarse - delta_eb) + 1e-9);  // converging
    CHECK(std::abs(d_fine - delta_eb) / delta_eb < 0.20);  // within 20% at fine
}
