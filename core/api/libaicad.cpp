// libaicad C ABI implementation — see libaicad.h.
//
// Dispatches string-in/string-out JSON calls to the C++ core. Today it serves:
//   ping             liveness + ABI echo (no kernel work; used by the host to
//                    confirm the DLL loaded and its OCCT deps resolved).
//   doc_compile_cpp  DIAGNOSTIC compile: takes a full document feature tree +
//                    env, runs aicad::doc::compile_document, writes the surviving
//                    bodies to an AICM file, and returns the per-feature statuses
//                    + body count + the AICM path. This is the strangler's
//                    parity probe (Step 2 of docs/CPP_MIGRATION.md): the host can
//                    feed the SAME tree it sends to the Python worker and diff the
//                    results, WITHOUT changing the production Python path.
//
// Everything here is exception-safe: any throw becomes a nonzero return with an
// {"error":...} payload so a bug in the C++ core can never crash the host.

#include "api/libaicad.h"

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/document.h"
#include "doc/session.h"
#include "doc/validate.h"
#include "ecad/session.h"
#include "sim/fea_modal.h"
#include "sim/fea_static.h"
#include "sim/lbm.h"
#include "sim/netlist.h"
#include "sim/spice.h"

using nlohmann::json;

namespace {

// malloc a NUL-terminated copy of `s` so the host frees it with aicad_free()
// (std::free). Returns nullptr only on allocation failure.
char* dup_cstr(const std::string& s) {
    char* p = static_cast<char*>(std::malloc(s.size() + 1));
    if (!p) return nullptr;
    std::memcpy(p, s.data(), s.size());
    p[s.size()] = '\0';
    return p;
}

// Emit `j` into *out_json (heap) and return `rc`. Always leaves *out_json valid.
int emit(json&& j, char** out_json, int rc) {
    if (out_json) *out_json = dup_cstr(j.dump());
    return rc;
}

int emit_error(const std::string& msg, char** out_json) {
    return emit(json{{"error", msg}}, out_json, 1);
}

// Choose where to write the AICM bytes. Honour an explicit params["aicm_out"]
// (the host/tests can pin a path), else a unique file in the OS temp dir. The
// counter + pid keep concurrent compiles from colliding without needing a clock
// (Date/now is intentionally avoided elsewhere in the codebase).
std::filesystem::path aicm_target(const json& params) {
    if (params.contains("aicm_out") && params["aicm_out"].is_string()) {
        return std::filesystem::path(params["aicm_out"].get<std::string>());
    }
    static std::atomic<unsigned long long> seq{0};
    unsigned long long n = seq.fetch_add(1);
    std::filesystem::path dir = std::filesystem::temp_directory_path();
    return dir / ("aicad_cpp_" + std::to_string(n) + ".aicm");
}

// doc_compile_cpp: compile a document tree to AICM + statuses (diagnostic).
int handle_compile(const json& params, char** out_json) {
    // The tree may arrive under "doc" (the {"features":[...]} shape compile_document
    // wants) or, for convenience, the host's tree may be passed directly. Accept
    // either: if there's a top-level "features", treat params itself as the doc.
    json doc;
    if (params.contains("doc")) {
        doc = params["doc"];
    } else if (params.contains("features")) {
        doc = params;
    } else {
        return emit_error("doc_compile_cpp: params need a 'doc' object (or a top-level 'features')", out_json);
    }
    json env = params.contains("env") ? params["env"] : json::object();

    aicad::doc::CompileResult res = aicad::doc::compile_document(doc, env);

    json statuses = json::array();
    for (const auto& s : res.statuses) {
        statuses.push_back(json{
            {"id", s.id},
            {"type", s.type},
            {"ok", s.ok},
            {"error", s.error},
        });
    }

    std::string aicm_path;
    if (!res.aicm.empty()) {
        std::filesystem::path target = aicm_target(params);
        std::ofstream f(target, std::ios::binary | std::ios::trunc);
        if (!f) {
            return emit_error("doc_compile_cpp: cannot open AICM output: " + target.string(), out_json);
        }
        f.write(reinterpret_cast<const char*>(res.aicm.data()),
                static_cast<std::streamsize>(res.aicm.size()));
        f.close();
        aicm_path = std::filesystem::absolute(target).string();
    }

    // Per-surviving-body stats (surfacing parity gate: area/face/edge counts for open
    // shells, volume for solids). Additive — existing volume fixtures are unaffected.
    json bodies = json::array();
    for (const auto& b : res.bodies_info) {
        bodies.push_back(json{
            {"name", b.name},
            {"kind", b.kind},
            {"volume", b.volume},
            {"area", b.area},
            {"n_faces", b.n_faces},
            {"n_edges", b.n_edges},
        });
    }

    return emit(json{
                    {"ok", res.ok},
                    {"body_count", res.body_count},
                    {"volume", res.volume},
                    {"bodies", bodies},
                    {"statuses", statuses},
                    {"owner_map", res.owner_map.is_null() ? json::object() : res.owner_map},
                    {"aicm_path", aicm_path},
                    {"aicm_bytes", static_cast<uint64_t>(res.aicm.size())},
                },
                out_json, 0);
}

}  // namespace

extern "C" {

AICAD_API const char* aicad_abi_version(void) { return "1"; }

AICAD_API void aicad_free(char* s) { std::free(s); }

AICAD_API int aicad_call(const char* method, const char* params_json, char** out_json) {
    if (out_json) *out_json = nullptr;
    if (!method) return emit_error("null method", out_json);

    std::string m(method);
    json params = json::object();
    try {
        if (params_json && params_json[0] != '\0') {
            params = json::parse(params_json);
        }
    } catch (const std::exception& e) {
        return emit_error(std::string("invalid params JSON: ") + e.what(), out_json);
    }

    try {
        if (m == "ping") {
            return emit(json{{"ok", true}, {"abi", aicad_abi_version()}, {"echo", params}},
                        out_json, 0);
        }
        if (m == "doc_compile_cpp") {
            return handle_compile(params, out_json);
        }
        if (m == "sim_fea_static") {
            // Linear-static FEA (engine/sim/fea_static.py parity probe). Params carry an
            // ALREADY-RESOLVED problem: points[n*3], tets[m*4], E_mpa, nu, yield_mpa,
            // fixed_nodes[], nodal_forces[n*3]. Returns displacement/von_mises/summary.
            auto getd = [&](const char* k) {
                std::vector<double> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<double>());
                return v;
            };
            auto geti = [&](const char* k) {
                std::vector<int> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<int>());
                return v;
            };
            aicad::sim::FeaStaticResult r = aicad::sim::fea_static_solve(
                getd("points"), geti("tets"), params.value("E_mpa", 0.0),
                params.value("nu", 0.0), params.value("yield_mpa", 250.0),
                geti("fixed_nodes"), getd("nodal_forces"));
            if (!r.ok) return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
            return emit(json{{"ok", true},
                             {"displacement", r.displacement},
                             {"disp_magnitude", r.disp_magnitude},
                             {"von_mises", r.von_mises},
                             {"n_dof", r.n_dof},
                             {"summary",
                              {{"max_von_mises_mpa", r.max_von_mises_mpa},
                               {"max_disp_mm", r.max_disp_mm},
                               {"min_fos", r.min_fos}}}},
                        out_json, 0);
        }
        if (m == "sim_fea_modal") {
            // Modal FEA (engine/sim/fea_modal.py parity probe). Params: points[n*3],
            // tets[m*4], E_mpa, nu, density_g_cm3, fixed_nodes[], n_modes. Returns
            // freqs_hz[] + mode_shapes[n_modes*n*3].
            auto getd = [&](const char* k) {
                std::vector<double> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<double>());
                return v;
            };
            auto geti = [&](const char* k) {
                std::vector<int> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<int>());
                return v;
            };
            aicad::sim::FeaModalResult r = aicad::sim::fea_modal_solve(
                getd("points"), geti("tets"), params.value("E_mpa", 0.0),
                params.value("nu", 0.0), params.value("density_g_cm3", 0.0),
                geti("fixed_nodes"), params.value("n_modes", 6));
            if (!r.ok) return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
            return emit(json{{"ok", true},
                             {"freqs_hz", r.freqs_hz},
                             {"mode_shapes", r.mode_shapes},
                             {"n_modes", r.n_modes},
                             {"n_nodes", r.n_nodes},
                             {"n_dof", r.n_dof}},
                        out_json, 0);
        }
        if (m == "sim_spice") {
            // Native MNA circuit sim (engine/sim/circuit.py parity probe; linear DC for
            // now). Params: {circuit:{...}, study:{...}}. Returns the sim.md result shape.
            const json& circ = params.contains("circuit") ? params["circuit"] : json::object();
            const json& study = params.contains("study") ? params["study"] : json::object();
            return emit(aicad::sim::spice_simulate(circ, study), out_json, 0);
        }
        if (m == "sim_netlist_parse") {
            // DIAGNOSTIC: parse a SPICE netlist deck text into the Circuit dict
            // (engine/sim/spice.py:parse_spice + subckt.py parity probe). Params:
            // {"netlist": "<deck text>"}. Returns the frozen Circuit dict (sim.md §1)
            // so the host's .cir flow can run on the C++ core (it then feeds sim_spice).
            std::string deck = params.contains("netlist") && params["netlist"].is_string()
                                   ? params["netlist"].get<std::string>()
                                   : std::string();
            return emit(aicad::sim::parse_netlist(deck), out_json, 0);
        }
        if (m == "sim_lbm") {
            // D3Q19 BGK LBM wind-tunnel kernel (engine/sim/lbm.py parity probe). The
            // trimesh voxelization is NOT done here: the caller passes an ALREADY-
            // precomputed solid mask + lattice params + step count so this isolates the
            // lattice numerics (the eventual CUDA payload). Contract:
            //   mask : {"nx","ny","nz","solid":[flat (Nx,Ny,Nz) row-major 0/1 ints]}
            //          solid[(x*Ny+y)*Nz+z] != 0 => solid cell.
            //   params : {"u_lat","tau0","voxel_mm","dx_m","dt_s","rho_air"}
            //   n_steps : number of LBM time steps.
            // Returns: {"ok", nx,ny,nz, ux,uy,uz,rho (flat (Nx,Ny,Nz) row-major),
            //           cd, drag_n, lift_n, frontal_area_mm2, steps_run, diverged}.
            const json& mask = params.contains("mask") ? params["mask"] : json::object();
            const json& pp = params.contains("params") ? params["params"] : json::object();
            int nx = mask.value("nx", 0);
            int ny = mask.value("ny", 0);
            int nz = mask.value("nz", 0);
            std::vector<uint8_t> solid;
            if (mask.contains("solid") && mask["solid"].is_array()) {
                solid.reserve(mask["solid"].size());
                for (const auto& v : mask["solid"]) solid.push_back(v.get<int>() ? 1 : 0);
            }
            aicad::sim::LbmParams lp;
            lp.u_lat = pp.value("u_lat", 0.05);
            lp.tau0 = pp.value("tau0", 1.0);
            lp.voxel_mm = pp.value("voxel_mm", 1.0);
            lp.dx_m = pp.value("dx_m", 1.0e-3);
            lp.dt_s = pp.value("dt_s", 1.0e-6);
            lp.rho_air = pp.value("rho_air", 1.225);
            int n_steps = params.value("n_steps", 0);
            aicad::sim::LbmResult r = aicad::sim::lbm_run(solid, nx, ny, nz, lp, n_steps);
            if (!r.ok) return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
            return emit(json{{"ok", true},
                             {"nx", r.nx},
                             {"ny", r.ny},
                             {"nz", r.nz},
                             {"ux", r.ux},
                             {"uy", r.uy},
                             {"uz", r.uz},
                             {"rho", r.rho},
                             {"cd", r.cd},
                             {"drag_n", r.drag_n},
                             {"lift_n", r.lift_n},
                             {"frontal_area_mm2", r.frontal_area_mm2},
                             {"steps_run", r.steps_run},
                             {"diverged", r.diverged}},
                        out_json, 0);
        }
        if (m == "doc_validate") {
            // DIAGNOSTIC: structural-validate a document tree (schema.validate_document
            // parity probe). Accepts {"doc":{...}} or a top-level {"features":[...]}.
            json doc;
            if (params.contains("doc")) doc = params["doc"];
            else if (params.contains("features")) doc = params;
            else return emit_error("doc_validate: params need a 'doc' object", out_json);
            std::vector<std::string> errs = aicad::doc::validate_document(doc);
            return emit(json{{"ok", errs.empty()}, {"errors", errs}}, out_json, 0);
        }
        // --- stateful document session RPCs (aicad::doc::session) ---------------
        // Each returns the host's ops-response JSON. An OpError becomes a nonzero
        // return carrying the {code,message,detail} envelope (so the host can treat
        // it as a structured error / fall back to Python).
        try {
            if (m == "doc_new")
                return emit(aicad::doc::doc_new(params.value("kind", std::string("part")),
                                                params.value("name", std::string())),
                            out_json, 0);
            if (m == "doc_apply_ops")
                return emit(aicad::doc::doc_apply_ops(
                                params.value("doc_id", std::string()),
                                params.contains("base_revision") && params["base_revision"].is_number()
                                    ? params["base_revision"].get<long>() : -1,
                                params.contains("ops") ? params["ops"] : json::array(),
                                params.value("coalesce", false)),
                            out_json, 0);
            if (m == "doc_undo")
                return emit(aicad::doc::doc_undo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_redo")
                return emit(aicad::doc::doc_redo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_tree")
                return emit(aicad::doc::doc_tree(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_rebuild")
                return emit(aicad::doc::doc_rebuild(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_close")
                return emit(json{{"closed", aicad::doc::doc_close(params.value("doc_id", std::string()))}},
                            out_json, 0);
            if (m == "doc_list")
                return emit(aicad::doc::doc_list(), out_json, 0);
            if (m == "doc_checkpoint")
                return emit(aicad::doc::doc_checkpoint(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_revert")
                return emit(aicad::doc::doc_revert(params.value("doc_id", std::string()),
                                                   params.value("checkpoint", std::string())),
                            out_json, 0);
            if (m == "config_list")
                return emit(aicad::doc::doc_config_list(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "config_add")
                return emit(aicad::doc::doc_config_add(
                                params.value("doc_id", std::string()),
                                params.value("name", std::string()),
                                params.contains("copy_from") ? params["copy_from"] : json(nullptr)),
                            out_json, 0);
            if (m == "config_delete")
                return emit(aicad::doc::doc_config_delete(params.value("doc_id", std::string()),
                                                          params.value("name", std::string())),
                            out_json, 0);
            if (m == "config_activate")
                return emit(aicad::doc::doc_config_activate(params.value("doc_id", std::string()),
                                                            params.value("name", std::string())),
                            out_json, 0);
            if (m == "config_set_override")
                return emit(aicad::doc::doc_config_set_override(
                                params.value("doc_id", std::string()),
                                params.value("name", std::string()),
                                params.value("feature_id", std::string()),
                                params.value("param", std::string()),
                                params.contains("value") ? params["value"] : json(nullptr),
                                params.value("clear", false)),
                            out_json, 0);
            if (m == "config_set_suppression")
                return emit(aicad::doc::doc_config_set_suppression(
                                params.value("doc_id", std::string()),
                                params.value("name", std::string()),
                                params.value("feature_id", std::string()),
                                params.value("suppressed", false)),
                            out_json, 0);
            if (m == "doc_save")
                return emit(aicad::doc::doc_save(params.value("doc_id", std::string()),
                                                 params.value("path", std::string())),
                            out_json, 0);
            if (m == "doc_open")
                return emit(aicad::doc::doc_open(params.value("path", std::string())), out_json, 0);
            if (m == "doc_resolve_pick")
                return emit(aicad::doc::doc_resolve_pick(
                                params.value("doc_id", std::string()),
                                params.value("body_id", 0),
                                params.value("kind", std::string()),
                                params.value("topo_id", 0)),
                            out_json, 0);
            if (m == "doc_push_pull")
                return emit(aicad::doc::doc_push_pull(
                                params.value("doc_id", std::string()),
                                params.value("body_id", 0),
                                params.value("topo_id", 0),
                                params.contains("distance") && params["distance"].is_number()
                                    ? params["distance"].get<double>() : 0.0,
                                params.value("commit", false)),
                            out_json, 0);
            if (m == "export_file")
                return emit(aicad::doc::doc_export_file(params.value("doc_id", std::string()),
                                                        params.value("path", std::string()),
                                                        params.value("format", std::string())),
                            out_json, 0);
            if (m == "import_file")
                return emit(aicad::doc::doc_import_file(params.value("path", std::string()),
                                                        params.value("into", std::string()),
                                                        params.value("as_solid", false),
                                                        params.value("format", std::string())),
                            out_json, 0);
        } catch (const aicad::doc::OpError& oe) {
            // Structured error envelope (matches the Python worker's doc_* errors).
            return emit(oe.envelope(), out_json, 3);
        }
        // --- stateful ECAD document session RPCs (aicad::ecad::session) ----------
        // Same contract as the doc_* block: an OpError becomes a nonzero return
        // carrying the {code,message,detail} envelope. The ECAD doc/ops layer is
        // pure JSON (no OCCT) — schematic + board document model + apply-ops.
        try {
            if (m == "ecad_new")
                return emit(aicad::ecad::ecad_new(params.value("kind", std::string("sch")),
                                                  params.value("name", std::string())),
                            out_json, 0);
            if (m == "ecad_apply_ops")
                return emit(aicad::ecad::ecad_apply_ops(
                                params.value("doc_id", std::string()),
                                params.contains("base_revision") && params["base_revision"].is_number()
                                    ? params["base_revision"].get<long>() : -1,
                                params.contains("ops") ? params["ops"] : json::array()),
                            out_json, 0);
            if (m == "ecad_undo")
                return emit(aicad::ecad::ecad_undo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_redo")
                return emit(aicad::ecad::ecad_redo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_tree")
                return emit(aicad::ecad::ecad_tree(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_rebuild")
                return emit(aicad::ecad::ecad_rebuild(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_close")
                return emit(json{{"closed", aicad::ecad::ecad_close(params.value("doc_id", std::string()))}},
                            out_json, 0);
            if (m == "ecad_describe")
                return emit(aicad::ecad::ecad_describe(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "ecad_schema")
                return emit(aicad::ecad::ecad_schema(params.value("kind", std::string("sch"))),
                            out_json, 0);
            if (m == "ecad_dfm") {
                json rules = params.contains("rules") ? params["rules"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    return emit(aicad::ecad::ecad_dfm_board(params["board"], rules), out_json, 0);
                return emit(aicad::ecad::ecad_dfm(params.value("doc_id", std::string()), rules),
                            out_json, 0);
            }
            if (m == "ecad_impedance")
                return emit(aicad::ecad::ecad_impedance(params), out_json, 0);
        } catch (const aicad::ecad::OpError& oe) {
            return emit(oe.envelope(), out_json, 3);
        }
        // Unknown to the C++ core -> nonzero so the host falls back to Python.
        return emit(json{{"error", "unknown method: " + m}}, out_json, 2);
    } catch (const std::exception& e) {
        return emit_error(std::string("aicad_call(") + m + ") threw: " + e.what(), out_json);
    } catch (...) {
        return emit_error("aicad_call(" + m + ") threw a non-std exception", out_json);
    }
}

}  // extern "C"
