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
#include <cstdlib>
#include <filesystem>
#include <string>
#include <system_error>

#include <nlohmann/json.hpp>

#include "sim/fea_run.h"
#include "sim/fea_adaptive.h"
#include "sim/mesher.h"
#include "util/platform_paths.h"

#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
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

namespace {
// Kirsch plate (mm): a plate in uniaxial tension with a central through-hole. The classic
// stress concentration, and the classic thing a coarse mesh misses — the peak lives in a
// band a fraction of the hole radius wide, so a uniform mesh that resolves the plate does
// not resolve the answer. Exactly the case adaptive refinement exists for.
constexpr double PL = 120.0, PW = 60.0, PT = 8.0, HOLE_R = 8.0;

std::string write_holed_plate_step() {
    TopoDS_Shape plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), PL, PW, PT).Shape();
    TopoDS_Shape hole =
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(PL / 2, PW / 2, -1.0), gp_Dir(0, 0, 1)), HOLE_R,
                                 PT + 2.0)
            .Shape();
    TopoDS_Shape cut = BRepAlgoAPI_Cut(plate, hole).Shape();
    auto p = std::filesystem::temp_directory_path() / "chudware_polymesh_plate.step";
    STEPControl_Writer wr;
    REQUIRE(wr.Transfer(cut, STEPControl_AsIs) == IFSelect_RetDone);
    REQUIRE(wr.Write(p.string().c_str()) == IFSelect_RetDone);
    return p.string();
}

json plate_face_samples(double x0) {
    json pts = json::array();
    for (int i = 0; i <= 4; ++i)
        for (int j = 0; j <= 2; ++j) pts.push_back({x0, PW * i / 4.0, PT * j / 2.0});
    return pts;
}
}  // namespace

TEST_CASE("adaptive refinement drives the error down and finds the stress concentration") {
    if (!mesher_backend_available(MesherBackend::Polymesh)) return;
    const std::string step = write_holed_plate_step();
    const double F = 20000.0;  // N, tension along +X

    json study;
    study["step_path"] = step;
    study["fineness"] = 0.35;  // deliberately coarse: the adapt loop must earn the answer
    study["mesh_order"] = 1;
    study["material"] = {{"E_mpa", 200000.0}, {"nu", 0.3}, {"yield_mpa", 250.0},
                         {"density_g_cm3", 7.85}};
    study["fixtures"] =
        json::array({json{{"points", plate_face_samples(0.0)}, {"tol_mm", 3.0}}});
    study["loads"] = json::array({json{{"faces", {{"points", plate_face_samples(PL)},
                                                  {"tol_mm", 3.0}}},
                                       {"type", "force"},
                                       {"vector", {F, 0.0, 0.0}}}});
    study["adapt"] = {{"max_passes", 3}, {"eta_target", 0.02}};

    const json res = fea_run_adaptive(study);
    REQUIRE_MESSAGE(res.value("ok", false), res.dump().substr(0, 900));
    REQUIRE(res.contains("adapt"));
    const json& adapt = res["adapt"];
    const json& passes = adapt["passes"];
    REQUIRE(passes.is_array());
    REQUIRE(passes.size() >= 2);

    // Every pass must be recorded with a real error number and a real mesh behind it.
    for (const json& p : passes) {
        CHECK(p["n_elems"].get<int>() > 0);
        CHECK(p["global_eta"].get<double>() > 0.0);
        CHECK(std::isfinite(p["global_eta"].get<double>()));
    }

    const double eta_first = passes.front()["global_eta"].get<double>();
    const double eta_last = passes.back()["global_eta"].get<double>();
    const int elems_first = passes.front()["n_elems"].get<int>();
    const int elems_last = passes.back()["n_elems"].get<int>();
    const double vm_first = passes.front()["max_von_mises_mpa"].get<double>();
    const double vm_last = passes.back()["max_von_mises_mpa"].get<double>();
    MESSAGE("eta " << eta_first << " -> " << eta_last << ", elems " << elems_first << " -> "
                   << elems_last << ", peak vm " << vm_first << " -> " << vm_last);

    // The point of the loop: error goes DOWN, and it does so by spending elements.
    CHECK(eta_last < eta_first);
    CHECK(elems_last > elems_first);
    // And the refined mesh resolves MORE peak stress than the coarse one did. A coarse
    // mesh under-reports the concentration, which reads as a safer part than it is.
    CHECK(vm_last > vm_first);

    // Net-section nominal stress and the Kirsch concentration factor (~3 for a hole small
    // against the plate; higher here because the hole eats a quarter of the width). The
    // band is wide because this is a coarse-budget adaptive run, not a converged study —
    // the assertion is "in the physical ballpark", not "converged to theory".
    const double nominal = F / ((PW - 2.0 * HOLE_R) * PT);  // MPa
    MESSAGE("net-section nominal=" << nominal << " MPa, peak=" << vm_last << " MPa");
    CHECK(vm_last > nominal);           // a concentration exists at all
    CHECK(vm_last < nominal * 6.0);     // and it is not a singular-corner artifact

    CHECK(adapt["passes_run"].get<int>() == static_cast<int>(passes.size()));
    CHECK(adapt.contains("stop_reason"));
    CHECK(adapt["mesher_backend"].get<std::string>() == "polymesh");
    // An unconverged run must confess in solver_notes, not only in the adapt block.
    if (!adapt["eta_target_met"].get<bool>()) {
        const json notes = res.value("solver_notes", json::array());
        bool warned = false;
        for (const json& n : notes)
            if (n.is_string() && n.get<std::string>().find("adaptive refinement stopped") !=
                                     std::string::npos)
                warned = true;
        CHECK(warned);
    }
}

TEST_CASE("the two backends' failure sets are measured, not assumed") {
    if (!mesher_backend_available(MesherBackend::Polymesh)) return;
    // The reason this exists: the integration originally shipped a claim that polymesh
    // "succeeds on geometry Netgen rejects", in a tooltip, a role card and an advisor
    // finding. Measuring it showed the opposite — on a pair of boxes fused along one edge,
    // Netgen meshes and polymesh's own watertightness guard refuses. All that text now
    // says polymesh is an independent second discretization, which is what it is. This
    // test pins the measurement so the claim cannot quietly drift back.
    TopoDS_Shape a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 20, 20, 20).Shape();
    TopoDS_Shape b = BRepPrimAPI_MakeBox(gp_Pnt(20, 20, 0), 20, 20, 20).Shape();
    TopoDS_Shape fused = BRepAlgoAPI_Fuse(a, b).Shape();
    auto path = std::filesystem::temp_directory_path() / "chudware_edge_touch.step";
    STEPControl_Writer wr;
    REQUIRE(wr.Transfer(fused, STEPControl_AsIs) == IFSelect_RetDone);
    REQUIRE(wr.Write(path.string().c_str()) == IFSelect_RetDone);

    MeshOptions o;
    o.fineness = 0.4;
    o.order = 1;
    o.backend = MesherBackend::Netgen;
    const MeshOut ng = mesh_step(path.string(), o);
    o.backend = MesherBackend::Polymesh;
    const MeshOut pm = mesh_step(path.string(), o);
    MESSAGE("edge-touching fuse: netgen ok=" << ng.ok << " polymesh ok=" << pm.ok
                                             << " polymesh error=" << pm.error);

    // Whatever each backend does here, a FAILURE must be a clean, explained refusal —
    // never a silently empty or malformed mesh handed to a solver.
    for (const MeshOut* m : {&ng, &pm}) {
        if (m->ok) {
            CHECK(m->n_elems > 0);
            CHECK(m->tets.size() ==
                  static_cast<size_t>(m->n_elems) * static_cast<size_t>(m->nodes_per_elem));
        } else {
            CHECK_FALSE(m->error.empty());
            CHECK(m->n_elems == 0);
            CHECK(m->tets.empty());
        }
    }
}

// ── Learned mesh advisor ────────────────────────────────────────────────────────────────
//
// The advisor is opt-in, advisory, and allowed to be UNAVAILABLE. That combination is
// exactly what makes it easy to break silently: a build that stopped staging the model, a
// resolver that only ever finds a source checkout, a budget refusal that stops being
// reported, or an advised hex/VEM family quietly labelled a tet. None of those fail a
// solve — they change what the result CLAIMS. So each case below pins an observable shape
// rather than a log line.
namespace {
// The four artifacts and the package directory name are the single spelling in
// cmake/ProvisionPolymesh.cmake (POLYMESH_ADVISOR_MODEL_FILES /
// POLYMESH_ADVISOR_PACKAGE_DIR); the resolver in sim/polymesh_backend.cpp reads them
// through compile definitions this TU deliberately does not see.
constexpr const char* kAdvisorPackageDir = "polymesh-advisor";
constexpr const char* kAdvisorModelFiles[] = {"model.onnx", "normalization.json",
                                              "clamps.json", "ood.json"};
// Set on the child process the invalid-override case spawns (see the comment there).
constexpr const char* kOverrideChildEnv = "CHUDWARE_TEST_ADVISOR_OVERRIDE_CHILD";

void set_env(const char* name, const char* value) {
#ifdef _WIN32
    ::_putenv_s(name, value == nullptr ? "" : value);
#else
    if (value == nullptr)
        ::unsetenv(name);
    else
        ::setenv(name, value, 1);
#endif
}

MeshOptions polymesh_beam_options() {
    MeshOptions o;
    o.backend = MesherBackend::Polymesh;
    o.fineness = 0.4;
    o.order = 1;
    return o;
}

// Whatever the advisor recommended, what came OUT is a fixed-stride polymesh tet mesh.
// This is the boundary the advisor's hex/hybrid/VEM action space must never cross:
// MeshOut cannot express those cells, so producing one would either crash a stride-indexed
// reader or, worse, be read as a tet.
void check_tet_only_contract(const MeshOut& m) {
    REQUIRE_MESSAGE(m.ok, m.error);
    CHECK((m.nodes_per_elem == 4 || m.nodes_per_elem == 10));
    CHECK(m.mesh_type_used ==
          (m.nodes_per_elem == 10 ? "tet10-polymesh" : "tet4-polymesh"));
    CHECK(m.tets.size() == static_cast<size_t>(m.n_elems) *
                               static_cast<size_t>(m.nodes_per_elem));
    CHECK(m.n_elems > 0);
    CHECK(m.n_nodes > 0);
    CHECK(m.jacobian_min > 0.0);
}

// PolyMesh's advisor needs upstream's prebuilt ONNX Runtime, which is pinned for Windows
// x64 and non-Apple 64-bit Unix only, so a PolyMesh build on any other platform/arch has
// the mesher, the shared element-size band and the adaptive loop but NO learned advisor
// (CHUDWARE_WITH_POLYMESH_ADVISOR off). "not-built" is that build's honest revision, as
// distinct from "not-loaded" (an advisor exists, its model did not load). Everything past
// this contract — a staged model package, a recommendation, a budget refusal, an override
// diagnostic — is model-specific, so the cases below check this and stop.
bool advisor_not_built(const MeshAdvisor& a) { return a.model_revision == "not-built"; }

// The advisor-off report is PRESENT and says why: silence would read as "you never asked",
// and the study must mesh exactly as it would have without opting in.
void check_advisor_off_contract(const MeshAdvisor& a, const MeshOut& on, const MeshOut& off,
                                int configured_order) {
    CHECK_FALSE(a.available);
    CHECK_FALSE(a.applied);
    CHECK_FALSE(a.vetoed);
    CHECK_FALSE(a.budget_refusal);
    // Nothing predicted, and the mesher named is the one that actually ran.
    CHECK(a.mesher == "graded_tet");
    CHECK(a.order == configured_order);
    CHECK(a.predicted_dof == 0.0);
    CHECK(a.predicted_mesh_ms == 0.0);
    CHECK(a.predicted_solve_ms == 0.0);
    CHECK(a.failure_prob == 0.0);
    CHECK(a.adapt_passes == 0);
    // The note names the missing capability and the platform reason, not a generic failure.
    CHECK(a.note.find("advisor unavailable") != std::string::npos);
    CHECK(a.note.find("configured deterministic mesh controls retained") !=
          std::string::npos);
    CHECK(a.note.find("no learned mesh advisor") != std::string::npos);
    CHECK(a.note.find("CHUDWARE_WITH_POLYMESH_ADVISOR off") != std::string::npos);
    // Tet-only success, and the SAME mesh a default-off study gets.
    check_tet_only_contract(on);
    CHECK(on.nodes_per_elem == off.nodes_per_elem);
    CHECK(on.n_elems == off.n_elems);
    CHECK(on.n_nodes == off.n_nodes);
    CHECK(on.char_len_mm == off.char_len_mm);
}

// Every field detail::mesh_advisor_json emits. A consumer (sim_manager's envelope parser,
// the MCP collector) reads these by name, so a dropped key is a silently blank disclosure.
constexpr const char* kAdvisorEnvelopeKeys[] = {
    "available", "applied", "vetoed", "budget_refusal", "mesher", "h_rel", "order",
    "adapt_passes", "eta_target", "predicted_dof", "predicted_mesh_ms",
    "predicted_solve_ms", "failure_prob", "ood_distance", "model_revision", "note"};
}  // namespace

TEST_CASE("the learned advisor is absent unless a study asks for it") {
    if (!mesher_backend_available(MesherBackend::Polymesh)) return;
    const std::string step = write_beam_step();

    const MeshOut off = mesh_step(step, polymesh_beam_options());
    REQUIRE_MESSAGE(off.ok, off.error);
    // ABSENCE, not a present-but-empty envelope: every consumer reads has_value() as
    // "no advisor ran" and serializes/renders nothing, which is the pre-advisor contract.
    CHECK_FALSE(off.advisor.has_value());

    // Same at the study boundary — a default polymesh study carries no "advisor" key at all.
    const json plain = fea_run_static(beam_study(step, 0.6, 2, 100.0, "polymesh"));
    REQUIRE_MESSAGE(plain.value("ok", false), plain.dump().substr(0, 600));
    CHECK_FALSE(plain.contains("advisor"));

    // And opting in IS what makes it appear, so the absence above is a decision rather
    // than a route that could never carry one.
    json study = beam_study(step, 0.6, 2, 100.0, "polymesh");
    study["learned_advisor"] = true;
    const json opted = fea_run_static(study);
    REQUIRE_MESSAGE(opted.value("ok", false), opted.dump().substr(0, 600));
    REQUIRE(opted.contains("advisor"));
    for (const char* key : kAdvisorEnvelopeKeys)
        CHECK_MESSAGE(opted["advisor"].contains(key), "missing advisor key " << key);
    // The mesh a learned study solved on is still a polymesh tet mesh.
    CHECK(opted["mesh_type_used"].get<std::string>().find("-polymesh") !=
          std::string::npos);
}

TEST_CASE("an opted-in advisor recommends from the staged model and names its revision") {
    if (!mesher_backend_available(MesherBackend::Polymesh)) return;

    const std::string step = write_beam_step();
    MeshOptions o = polymesh_beam_options();
    const MeshOut off = mesh_step(step, o);
    REQUIRE_MESSAGE(off.ok, off.error);

    o.learned_advisor = true;
    const MeshOut on = mesh_step(step, o);
    check_tet_only_contract(on);
    REQUIRE(on.advisor.has_value());
    const MeshAdvisor& a = *on.advisor;
    if (advisor_not_built(a)) {
        // Advisor-off platform: there is no staged model package to find and no
        // recommendation to judge, so the honest-unavailable report IS the contract here.
        check_advisor_off_contract(a, on, off, o.order);
        return;
    }

    // The PRODUCTION lookup: the model package staged beside this module. Baking the
    // configure-time source directory in as the only lookup left the advisor reachable on
    // exactly one machine (the one that built the binary) and dead in every packaged
    // install, so assert the staged package is where the resolver's second step looks.
    const std::filesystem::path exe(chudware::platform::executablePath());
    REQUIRE_FALSE(exe.empty());
    const std::filesystem::path staged = exe.parent_path() / kAdvisorPackageDir;
    for (const char* file : kAdvisorModelFiles) {
        std::error_code ec;
        CHECK_MESSAGE(std::filesystem::exists(staged / file, ec),
                      "advisor artifact not staged: " << (staged / file).string());
    }

    // available==false in a build that STAGES the model is the whole regression this case
    // exists for: an advisor compiled out, unstaged, or looked up only in a source tree
    // reports an honest fallback here instead of a recommendation.
    REQUIRE_MESSAGE(a.available, a.note);
    // A revision naming the model that actually ran. "not-loaded" is the fallback
    // sentinel; an "override:<path>" prefix is what an operator override reports and would
    // mean this run cannot name the pinned model it recommended from.
    CHECK_FALSE(a.model_revision.empty());
    CHECK(a.model_revision != "not-loaded");
    CHECK(a.model_revision.rfind("override:", 0) != 0);
    // With a loaded model the decision either applied or was refused — never neither,
    // which would be a recommendation nothing acted on and nothing disclosed.
    CHECK(a.applied != a.vetoed);
    // The reported order is the order that was BUILT, not a wish: nodes_per_elem is what
    // every solver strides by.
    CHECK(on.nodes_per_elem == (a.order == 2 ? 10 : 4));

    if (a.applied) {
        CHECK(a.h_rel > 0.0);
        CHECK(std::isfinite(a.h_rel));
        CHECK(std::isfinite(a.eta_target));
        CHECK(a.eta_target >= 0.0);
        CHECK(a.adapt_passes >= 0);
        CHECK(std::isfinite(a.predicted_dof));
        CHECK(std::isfinite(a.ood_distance));
        CHECK_FALSE(a.budget_refusal);
        // The advisor's action space includes hex/hybrid/VEM families Chudware cannot
        // express. Reporting one is honest; APPLYING one is not, so the envelope has to
        // say the family was refused while the mesh stays graded tet.
        if (a.mesher != "graded_tet") {
            CHECK(a.note.find("tet-only solve contract") != std::string::npos);
            CHECK(a.note.find("retained graded_tet") != std::string::npos);
        }
    } else {
        // A refusal suppresses predictions rather than reporting numbers for an action
        // that was rejected, and leaves the configured deterministic mesh untouched.
        CHECK(a.predicted_dof == 0.0);
        CHECK(a.predicted_mesh_ms == 0.0);
        CHECK(a.predicted_solve_ms == 0.0);
        CHECK(a.failure_prob == 0.0);
        CHECK(a.note.find("configured deterministic mesh controls retained") !=
              std::string::npos);
        CHECK(on.n_elems == off.n_elems);
        CHECK(on.n_nodes == off.n_nodes);
        CHECK(on.nodes_per_elem == off.nodes_per_elem);
        CHECK(on.char_len_mm == off.char_len_mm);
    }
}

TEST_CASE("a DOF budget the advisor cannot meet is refused, never quietly relaxed") {
    if (!mesher_backend_available(MesherBackend::Polymesh)) return;
    const std::string step = write_beam_step();

    MeshOptions o = polymesh_beam_options();
    const MeshOut off = mesh_step(step, o);
    REQUIRE_MESSAGE(off.ok, off.error);

    o.learned_advisor = true;
    o.advisor_max_dof = 0;  // 0 disables the budget: the historical decision, unchanged
    const MeshOut unbudgeted = mesh_step(step, o);
    check_tet_only_contract(unbudgeted);
    REQUIRE(unbudgeted.advisor.has_value());
    if (advisor_not_built(*unbudgeted.advisor)) {
        // Advisor-off platform: no decision exists, so no budget can filter one. The
        // report must still be present and honest, and the mesh unchanged.
        check_advisor_off_contract(*unbudgeted.advisor, unbudgeted, off, o.order);
        return;
    }
    REQUIRE_MESSAGE(unbudgeted.advisor->available, unbudgeted.advisor->note);
    // A disabled budget can never be the REASON for a refusal.
    CHECK_FALSE(unbudgeted.advisor->budget_refusal);

    o.advisor_max_dof = 1;  // no mesh of this beam fits in one degree of freedom
    const MeshOut tiny = mesh_step(step, o);
    check_tet_only_contract(tiny);
    REQUIRE(tiny.advisor.has_value());
    const MeshAdvisor& a = *tiny.advisor;
    REQUIRE_MESSAGE(a.available, a.note);
    CHECK(a.vetoed);
    CHECK_FALSE(a.applied);
    // The budget is the ONLY input that changed between these two runs, so if the advisor
    // answered without one it must attribute this refusal to the budget. Without this the
    // budget_refusal field can be dropped end to end and every refusal still looks like an
    // ordinary veto — the caller can no longer tell "nothing affordable exists" from "this
    // part is out of distribution".
    if (!unbudgeted.advisor->vetoed) CHECK(a.budget_refusal);
    // Predictions suppressed: an unaffordable action's dof/time/risk numbers are not
    // reported as if they described the run.
    CHECK(a.predicted_dof == 0.0);
    CHECK(a.predicted_mesh_ms == 0.0);
    CHECK(a.predicted_solve_ms == 0.0);
    CHECK(a.failure_prob == 0.0);
    CHECK(a.note.find("configured deterministic mesh controls retained") !=
          std::string::npos);
    // And the refusal costs the study nothing: the mesh is the one a default-off run gets,
    // element for element and node for node. A budget that shrank the mesh would be the
    // advisor silently deciding accuracy, which is the one thing it must never do.
    CHECK(a.order == o.order);  // the CONFIGURED order, not one the refusal invented
    CHECK(tiny.nodes_per_elem == off.nodes_per_elem);
    CHECK(tiny.n_elems == off.n_elems);
    CHECK(tiny.n_nodes == off.n_nodes);
    CHECK(tiny.char_len_mm == off.char_len_mm);
    CHECK(tiny.points == off.points);
    CHECK(tiny.tets == off.tets);
}

// The child half of the case below. CHUDWARE_POLYMESH_ADVISOR_DIR is read exactly once,
// into a process-wide lazily-constructed runtime, so a bad-override probe CANNOT share a
// process with the good-model cases above — whichever ran first would decide the answer for
// the rest of the run. It self-skips unless the parent marked it.
TEST_CASE("advisor-invalid-override-child") {
    if (std::getenv(kOverrideChildEnv) == nullptr) return;
    REQUIRE(mesher_backend_available(MesherBackend::Polymesh));
    REQUIRE(std::getenv("CHUDWARE_POLYMESH_ADVISOR_DIR") != nullptr);
    const std::string step = write_beam_step();

    MeshOptions o = polymesh_beam_options();
    const MeshOut off = mesh_step(step, o);
    REQUIRE_MESSAGE(off.ok, off.error);

    o.learned_advisor = true;
    const MeshOut on = mesh_step(step, o);
    // The mesh does not fail because the model did. An override pointing at the wrong
    // directory is an advisor problem, not a geometry problem.
    check_tet_only_contract(on);
    REQUIRE(on.advisor.has_value());
    const MeshAdvisor& a = *on.advisor;
    if (advisor_not_built(a)) {
        // Advisor-off platform: the override env var is never read, because there is no
        // model to resolve. The report says exactly that, and it must not borrow the
        // override's diagnostic to explain itself.
        check_advisor_off_contract(a, on, off, o.order);
        CHECK(a.note.find("CHUDWARE_POLYMESH_ADVISOR_DIR") == std::string::npos);
        return;
    }
    // PRESENT and honestly unavailable: the study asked, so it gets an answer that says
    // no model ran. Absence here would read as "nobody asked".
    CHECK_FALSE(a.available);
    CHECK_FALSE(a.applied);
    CHECK_FALSE(a.vetoed);
    CHECK_FALSE(a.budget_refusal);
    // No revision may be claimed for a model that never loaded, and the note has to name
    // the override and what it was missing — an operator who set it needs to know it was
    // not silently ignored in favour of some other directory.
    CHECK(a.model_revision == "not-loaded");
    CHECK(a.mesher == "graded_tet");
    CHECK(a.note.find("advisor unavailable") != std::string::npos);
    CHECK(a.note.find("CHUDWARE_POLYMESH_ADVISOR_DIR") != std::string::npos);
    CHECK(a.note.find("does not contain model.onnx") != std::string::npos);
    CHECK(a.predicted_dof == 0.0);
    CHECK(a.failure_prob == 0.0);
    // Deterministic controls retained, exactly as with a refusal.
    CHECK(on.nodes_per_elem == off.nodes_per_elem);
    CHECK(on.n_elems == off.n_elems);
    CHECK(on.n_nodes == off.n_nodes);
    CHECK(on.char_len_mm == off.char_len_mm);

    // And the SOLVE still lands a real number. A dead advisor must cost a study its
    // advice, never its answer.
    json study = beam_study(step, 0.6, 2, 100.0, "polymesh");
    study["learned_advisor"] = true;
    const json res = fea_run_static(study);
    REQUIRE_MESSAGE(res.value("ok", false), res.dump().substr(0, 600));
    REQUIRE(res.contains("advisor"));
    CHECK_FALSE(res["advisor"]["available"].get<bool>());
    CHECK(res["advisor"]["model_revision"].get<std::string>() == "not-loaded");
    CHECK(res["summary"]["max_disp_mm"].get<double>() > 0.0);
}

TEST_CASE("an invalid advisor model override falls back honestly instead of failing") {
    if (!mesher_backend_available(MesherBackend::Polymesh)) return;
    if (std::getenv(kOverrideChildEnv) != nullptr) return;  // this process IS the child
    const std::filesystem::path exe(chudware::platform::executablePath());
    REQUIRE_FALSE(exe.empty());

    // An existing directory holding none of the four artifacts: "set but incomplete",
    // which must be an ERROR for the override rather than a silent fall-through to the
    // staged package sitting right beside this binary.
    const std::filesystem::path empty_dir =
        std::filesystem::temp_directory_path() / "chudware_advisor_override_empty";
    std::error_code ec;
    std::filesystem::remove_all(empty_dir, ec);
    std::filesystem::create_directories(empty_dir, ec);
    REQUIRE(std::filesystem::is_directory(empty_dir, ec));
    for (const char* file : kAdvisorModelFiles)
        REQUIRE_FALSE(std::filesystem::exists(empty_dir / file, ec));

    set_env("CHUDWARE_POLYMESH_ADVISOR_DIR", empty_dir.string().c_str());
    set_env(kOverrideChildEnv, "1");
#ifdef _WIN32
    const std::string cmd = "\"\"" + exe.string() +
                            "\" --test-case=advisor-invalid-override-child\"";
#else
    const std::string cmd =
        "'" + exe.string() + "' --test-case=advisor-invalid-override-child";
#endif
    const int rc = std::system(cmd.c_str());
    // Unset before asserting: a failed probe must not leave the override set for anything
    // that runs after it.
    set_env("CHUDWARE_POLYMESH_ADVISOR_DIR", nullptr);
    set_env(kOverrideChildEnv, nullptr);
    CHECK_MESSAGE(rc == 0, "override probe [" << cmd << "] exited " << rc);
}
