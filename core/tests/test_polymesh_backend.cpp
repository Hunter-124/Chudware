// test_polymesh_backend — the polymesh (Polyhedral-FEA) mesher backend must be
// interchangeable with Netgen, not merely present.
//
// A second mesher is only worth having if the ANSWER does not depend on which one ran.
// So this mirrors test_netgen_mesh's cantilever and asserts three things:
//   • polymesh honors the shared MeshOut contract (stride, quality block, counts) for
//     tet4 and tet10, including the mid-edge slot mapping — a wrong tet10 permutation
//     does not crash, it silently warps the element, so it is checked by SOLVING;
//   • fineness moves the element count in the same direction it does for Netgen;
//   • a cantilever solved on a polymesh mesh lands on the same Euler-Bernoulli tip
//     deflection as one solved on a Netgen mesh, within mesher-to-mesher tolerance.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <filesystem>
#include <string>

#include <nlohmann/json.hpp>

#include "sim/fea_run.h"
#include "sim/mesher.h"

#include <BRepPrimAPI_MakeBox.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Writer.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>

using namespace chudware::sim;
using nlohmann::json;

namespace {
// Same cantilever as test_netgen_mesh (mm): along X, square WxH cross-section.
constexpr double L = 100.0, W = 10.0, H = 10.0;

std::string write_beam_step() {
    TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), L, W, H).Shape();
    auto p = std::filesystem::temp_directory_path() / "chudware_polymesh_beam.step";
    STEPControl_Writer wr;
    REQUIRE(wr.Transfer(box, STEPControl_AsIs) == IFSelect_RetDone);
    REQUIRE(wr.Write(p.string().c_str()) == IFSelect_RetDone);
    return p.string();
}

json face_samples(double x0) {
    json pts = json::array();
    for (int i = 0; i <= 4; ++i)
        for (int j = 0; j <= 4; ++j) pts.push_back({x0, W * i / 4.0, H * j / 4.0});
    return pts;
}

json beam_study(const std::string& step, double fineness, int order, double F,
                const char* backend) {
    json s;
    s["step_path"] = step;
    s["fineness"] = fineness;
    s["mesh_order"] = order;
    s["mesher_backend"] = backend;
    s["material"] = {{"E_mpa", 200000.0}, {"nu", 0.29}, {"yield_mpa", 250.0},
                     {"density_g_cm3", 7.85}};
    // A stair-cased Cartesian fill lands its boundary nodes near, not exactly on, the
    // nominal face plane, so the face tolerance has to admit a fraction of an element
    // rather than the 0.5 mm the exact-BRep Netgen path can use.
    s["fixtures"] = json::array({json{{"points", face_samples(0.0)}, {"tol_mm", 2.0}}});
    s["loads"] = json::array({json{{"faces", {{"points", face_samples(L)}, {"tol_mm", 2.0}}},
                                   {"type", "force"},
                                   {"vector", {0.0, 0.0, -F}}}});
    return s;
}
}  // namespace

TEST_CASE("polymesh backend honors the MeshOut contract for tet4 and tet10") {
    if (!mesher_backend_available(MesherBackend::Polymesh)) return;  // build without polymesh
    const std::string step = write_beam_step();

    MeshOptions o;
    o.backend = MesherBackend::Polymesh;
    o.fineness = 0.4;
    o.order = 1;
    MeshOut t4 = mesh_step(step, o);
    REQUIRE_MESSAGE(t4.ok, t4.error);
    CHECK(t4.nodes_per_elem == 4);
    CHECK(t4.n_elems > 0);
    CHECK(t4.n_nodes > 0);
    CHECK(t4.tets.size() == static_cast<size_t>(t4.n_elems) * 4);
    CHECK(t4.mesh_type_used == "tet4-polymesh");
    // The quality block is filled by the SAME code the Netgen path uses, so these mean
    // exactly what they mean there: no inverted elements, finite aspect, real volume.
    CHECK(t4.jacobian_min > 0.0);
    CHECK(std::isfinite(t4.aspect_max));
    CHECK(t4.aspect_avg >= 1.0);
    CHECK(t4.min_tet_volume_mm3 > 0.0);
    CHECK(t4.aspect_hist.size() == 10);

    o.order = 2;
    MeshOut t10 = mesh_step(step, o);
    REQUIRE_MESSAGE(t10.ok, t10.error);
    CHECK(t10.nodes_per_elem == 10);
    CHECK(t10.tets.size() == static_cast<size_t>(t10.n_elems) * 10);
    CHECK(t10.mesh_type_used == "tet10-polymesh");
    // fill_quality scores the CORNER tets, so a scrambled mid-edge permutation would slip
    // past it — the solve in the next case is what actually pins the slot mapping.
    CHECK(t10.jacobian_min > 0.0);

    o.order = 1;
    o.fineness = 0.85;
    MeshOut fine = mesh_step(step, o);
    REQUIRE_MESSAGE(fine.ok, fine.error);
    CHECK(fine.n_elems > t4.n_elems);
}

TEST_CASE("a cantilever reads the same deflection on either mesher") {
    if (!mesher_backend_available(MesherBackend::Polymesh)) return;
    const std::string step = write_beam_step();
    const double F = 100.0;                                 // N
    const double I = W * H * H * H / 12.0;                  // mm^4
    const double E = 200000.0;                              // MPa
    const double delta_eb = F * L * L * L / (3.0 * E * I);  // Euler-Bernoulli tip, mm

    json netgen = fea_run_static(beam_study(step, 0.6, 2, F, "netgen"));
    json poly = fea_run_static(beam_study(step, 0.6, 2, F, "polymesh"));
    REQUIRE_MESSAGE(netgen.value("ok", false), netgen.dump());
    REQUIRE_MESSAGE(poly.value("ok", false), poly.dump());

    const double d_netgen = netgen["summary"]["max_disp_mm"].get<double>();
    const double d_poly = poly["summary"]["max_disp_mm"].get<double>();
    MESSAGE("analytic=" << delta_eb << " netgen=" << d_netgen << " polymesh=" << d_poly);

    CHECK(d_poly > 0.0);
    // Within 30% of the analytic tip deflection. A wrong tet10 mid-edge permutation
    // produces a wildly different (usually far stiffer) beam, so this is the real guard
    // on kTet10PolymeshToChudware, and the band is wide enough that a stair-cased
    // boundary's genuine extra stiffness is not mistaken for a bug.
    CHECK(std::abs(d_poly - delta_eb) / delta_eb < 0.30);
    // And the two meshers must agree with EACH OTHER more closely than either agrees
    // with beam theory would allow by luck.
    CHECK(std::abs(d_poly - d_netgen) / delta_eb < 0.30);
}
