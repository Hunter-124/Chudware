// fea_profile — standalone profiling harness for the static-stress FEA path.
//
// Reproduces the GUI "rectangular beam bending" study OUTSIDE the daemon/GUI (so no IPC
// lock convoy, no viewport draw) and instruments the pure numerical solve two ways:
//   1. Phase wall-clock split via CHUDWARE_FEA_PROFILE (mesh / assemble / factorize /
//      back-substitute / stress-recovery) — printed by fea_run_static + fea_static_solve.
//   2. The in-process SAMPLING profiler (core/prof) wrapped around the whole run, so we see
//      WHICH function eats the solve (Eigen factorize vs assembly vs meshing).
//
// It sweeps the mesh-fineness slider so the scaling curve (n_dof, time, memory) is explicit
// — this is the data needed to decide the fix (solver swap vs default cap vs async).
//
// Build: part of core/build-occt-linux (CHUDWARE_WITH_OCCT). Run:
//   CHUDWARE_FEA_PROFILE=1 ./bin/fea_profile [L W H  f0 f1 ...]
// e.g.  CHUDWARE_FEA_PROFILE=1 ./bin/fea_profile 200 20 20 0.3 0.5 0.7 0.85 1.0
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <filesystem>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "prof/sampler.h"
#include "sim/fea_run.h"

#include <BRepPrimAPI_MakeBox.hxx>
#include <STEPControl_Writer.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>

using namespace chudware::sim;
using nlohmann::json;

static double gL = 200.0, gW = 20.0, gH = 20.0;  // beam dims (mm), overridable by argv

static std::string write_beam_step() {
    TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gL, gW, gH).Shape();
    auto p = std::filesystem::temp_directory_path() / "chudware_fea_profile_beam.step";
    STEPControl_Writer wr;
    wr.Transfer(box, STEPControl_AsIs);
    wr.Write(p.string().c_str());
    return p.string();
}

// Grid of sample points over the x=x0 end face (matches the GUI faceset pick).
static json face_samples(double x0) {
    json pts = json::array();
    for (int i = 0; i <= 4; ++i)
        for (int j = 0; j <= 4; ++j)
            pts.push_back({x0, gW * i / 4.0, gH * j / 4.0});
    return pts;
}

// Cantilever bending: fixed at x=0, transverse -Z force at x=L (the exact GUI default study).
static json beam_study(const std::string& step, double fineness) {
    json s;
    s["step_path"] = step;
    s["fineness"] = fineness;
    s["mesh_order"] = 2;  // GUI static default = High-quality (tet10)
    s["material"] = {{"E_mpa", 200000.0}, {"nu", 0.29}, {"yield_mpa", 250.0},
                     {"density_g_cm3", 7.85}};
    s["fixtures"] = json::array({json{{"points", face_samples(0.0)}, {"tol_mm", 1.0}}});
    s["loads"] = json::array({json{{"faces", {{"points", face_samples(gL)}, {"tol_mm", 1.0}}},
                                    {"type", "force"},
                                    {"vector", {0.0, 0.0, -500.0}}}});
    return s;
}

int main(int argc, char** argv) {
    std::vector<double> fins;
    if (argc >= 4) { gL = std::atof(argv[1]); gW = std::atof(argv[2]); gH = std::atof(argv[3]); }
    for (int i = 4; i < argc; ++i) fins.push_back(std::atof(argv[i]));
    if (fins.empty()) fins = {0.3, 0.5, 0.7, 0.85, 1.0};

    const std::string step = write_beam_step();
    std::fprintf(stderr, "=== fea_profile: beam %.0fx%.0fx%.0f mm, tet10, sweep fineness ===\n",
                 gL, gW, gH);

    // Start the built-in sampling profiler (1 kHz). TargetScope below registers the worker
    // thread (here: main) so its stacks are sampled during the solves.
    chudware::prof::start(/*interval_us=*/1000, /*mode=*/"all");
    {
        chudware::prof::TargetScope scope(true);
        for (double f : fins) {
            std::fprintf(stderr, "\n----- fineness = %.2f -----\n", f);
            json out = fea_run_static(beam_study(step, f));
            if (!out.value("ok", false)) {
                std::fprintf(stderr, "[fea] SOLVE FAILED: %s\n",
                             out.value("error", std::string("?")).c_str());
                continue;
            }
            std::fprintf(stderr, "[fea] result: n_dof=%d  max_vm=%.3f MPa  max_disp=%.4f mm\n",
                         out.value("n_dof", 0),
                         out["summary"].value("max_von_mises_mpa", 0.0),
                         out["summary"].value("max_disp_mm", 0.0));
        }
    }
    chudware::prof::stop();

    // Symbolize + emit the sampling report: flat top-frames to stderr, folded stacks to a file
    // for flamegraph/speedscope.
    const std::string folded =
        (std::filesystem::temp_directory_path() / "fea_profile.folded").string();
    json rep = chudware::prof::report(json{{"write_folded", folded}, {"top", 25}});
    std::fprintf(stderr, "\n=== sampling profiler: hottest frames ===\n%s\n",
                 rep.dump(2).c_str());
    std::fprintf(stderr, "folded stacks -> %s\n", folded.c_str());
    return 0;
}
