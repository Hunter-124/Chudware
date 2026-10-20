// Durable sim solve-state cache gate (F1).
//
// (a) FEA static twice -> 2nd is HIT, byte-identical, print wall-times
// (b) mutate load/material/mesh -> MISS
// (c) SPICE + CFD round-trip + invalidate-on-input-change
// (d) .step+ save->reopen restores the cached study result
//
// Deterministic fixed inputs. Wall-times are printed (no expected-time asserts).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <map>
#include <fstream>
#include <iterator>
#include <sstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include <miniz.h>

#include "environment_test_support.h"
#include "doc/session.h"
#include "doc/stepplus.h"
#include "doc/native_policy.h"
#include "sim/fea_run.h"
#include "sim/flow_run.h"
#include "sim/numpy_rng.h"
#include "sim/sim_cancel.h"
#include "sim/solver_threads.h"
#include "sim/sim_cache.h"
#include "sim/spice.h"

using nlohmann::json;
using chudware::sim::cache::Kind;
namespace fs = std::filesystem;

namespace {

double ms_since(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
        .count();
}

// Inline 1x1x1 cube (6 tets) — same fixture shape as test_fatigue (OCCT-free mesh).
json cube_static_study(double force_z = 50000.0, double E = 200000.0) {
    json study;
    study["points"] = {0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
                       0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1};
    study["tets"] = {0, 1, 2, 6, 0, 2, 3, 6, 0, 3, 7, 6,
                     0, 7, 4, 6, 0, 4, 5, 6, 0, 5, 1, 6};
    study["material"] = {{"E_mpa", E}, {"nu", 0.29}, {"yield_mpa", 370.0}};
    study["fixtures"] = {{{"points", {{0, 0, 0}, {1, 0, 0}, {1, 1, 0}, {0, 1, 0}}},
                          {"tol_mm", 0.01}}};
    study["loads"] = {{{"type", "force"},
                       {"vector", {0.0, 0.0, force_z}},
                       {"faces", {{"points", {{0, 0, 1}, {1, 0, 1}, {1, 1, 1}, {0, 1, 1}}},
                                  {"tol_mm", 0.01}}}}};
    return study;
}

// A cantilever beam big enough that the summary's two-element fixture ring does NOT
// swallow the whole mesh (the cube above has exactly one element layer, so its ring
// hits the documented whole-field fallback). 10x2x2 hex cells -> 6 Freudenthal tets
// each; fixed at x=0, pushed at x=Lx, so the nodal stress peak is AT the clamp — the
// node the summary reduction excludes.
json beam_static_study(const std::string& vtu_path, double force_z = -1000.0) {
    const int nx = 10, ny = 2, nz = 2;
    const double Lx = 100.0, Ly = 10.0, Lz = 10.0;
    const int NX = nx + 1, NY = ny + 1, NZ = nz + 1;
    auto gid = [&](int i, int j, int k) { return (k * NY + j) * NX + i; };
    std::vector<double> pts(static_cast<size_t>(NX) * NY * NZ * 3, 0.0);
    for (int k = 0; k < NZ; ++k)
        for (int j = 0; j < NY; ++j)
            for (int i = 0; i < NX; ++i) {
                const size_t id = static_cast<size_t>(gid(i, j, k));
                pts[id * 3 + 0] = Lx * i / nx;
                pts[id * 3 + 1] = Ly * j / ny;
                pts[id * 3 + 2] = Lz * k / nz;
            }
    static const int TET[6][4] = {{0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
                                  {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
    std::vector<int> tets;
    for (int k = 0; k < nz; ++k)
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                const int c[8] = {gid(i, j, k),         gid(i + 1, j, k),
                                  gid(i + 1, j + 1, k), gid(i, j + 1, k),
                                  gid(i, j, k + 1),     gid(i + 1, j, k + 1),
                                  gid(i + 1, j + 1, k + 1), gid(i, j + 1, k + 1)};
                for (auto& tt : TET)
                    for (int v = 0; v < 4; ++v) tets.push_back(c[tt[v]]);
            }
    json fixture_pts = json::array(), load_pts = json::array();
    for (size_t id = 0; id * 3 + 2 < pts.size(); ++id) {
        const double x = pts[id * 3], y = pts[id * 3 + 1], z = pts[id * 3 + 2];
        if (x < 1e-9) fixture_pts.push_back(json::array({x, y, z}));
        if (std::abs(x - Lx) < 1e-9) load_pts.push_back(json::array({x, y, z}));
    }
    json study;
    study["points"] = pts;
    study["tets"] = tets;
    study["nodes_per_elem"] = 4;
    study["material"] = {{"E_mpa", 200000.0}, {"nu", 0.3}, {"yield_mpa", 250.0}};
    study["fixtures"] = json::array(
        {json{{"points", fixture_pts}, {"tol_mm", 1e-6}, {"sid", "Ftestfix"}}});
    study["loads"] = json::array(
        {json{{"type", "force"},
              {"vector", json::array({0.0, 0.0, force_z})},
              {"faces", json{{"points", load_pts}, {"tol_mm", 1e-6}}}}});
    if (!vtu_path.empty()) study["vtu_path"] = vtu_path;
    return study;
}

json simple_spice_circuit(double R = 1000.0, double V = 1.0) {
    return json{
        {"nodes", json::array({"0", "OUT", "SRC"})},
        {"devices",
         json::array({
             json{{"kind", "V"},
                  {"name", "VS"},
                  {"nodes", json::array({"SRC", "0"})},
                  {"value", V},
                  {"params", json::object()}},
             json{{"kind", "R"},
                  {"name", "RS"},
                  {"nodes", json::array({"SRC", "OUT"})},
                  {"value", R},
                  {"params", json::object()}},
             json{{"kind", "R"},
                  {"name", "RL"},
                  {"nodes", json::array({"OUT", "0"})},
                  {"value", R},
                  {"params", json::object()}},
         })},
        {"models", json::object()},
        {"params", json{{"TEMP", 27}}},
    };
}

json spice_op_study() {
    return json{{"analyses", json::array({json{{"type", "op"}}})},
                {"probes", json::array({"V(OUT)"})}};
}

// Unit cube triangle soup for CFD (8 corners, 12 tris).
void unit_cube_tris(std::vector<float>& pos, std::vector<uint32_t>& idx) {
    pos = {
        0, 0, 0, 50, 0, 0, 50, 50, 0, 0, 50, 0,  // bottom z=0
        0, 0, 50, 50, 0, 50, 50, 50, 50, 0, 50, 50,
    };
    idx = {
        0, 1, 2, 0, 2, 3,  // bottom
        4, 6, 5, 4, 7, 6,  // top
        0, 4, 5, 0, 5, 1,  // -y
        3, 2, 6, 3, 6, 7,  // +y
        0, 3, 7, 0, 7, 4,  // -x
        1, 5, 6, 1, 6, 2,  // +x
    };
}

fs::path tmp_path(const std::string& name) {
    return fs::temp_directory_path() / ("chudware_test_sim_cache_" + name);
}

}  // namespace
void write_zip(const fs::path& path,
               const std::vector<std::pair<std::string, std::string>>& entries,
               mz_uint compression = MZ_DEFAULT_COMPRESSION) {
    mz_zip_archive zip{};
    REQUIRE(mz_zip_writer_init_file(&zip, path.string().c_str(), 0));
    for (const auto& [name, data] : entries) {
        REQUIRE(mz_zip_writer_add_mem(&zip, name.c_str(), data.data(), data.size(),
                                      compression));
    }
    REQUIRE(mz_zip_writer_finalize_archive(&zip));
    REQUIRE(mz_zip_writer_end(&zip));
}

void check_stepplus_limit(const fs::path& path, const std::string& expected) {
    try {
        (void)chudware::doc::stepplus_open(path.string());
        FAIL("malicious archive was accepted");
    } catch (const chudware::doc::OpError& e) {
        CHECK(e.code == "io_error");
        CHECK(e.detail.at("limit") == expected);
    }
    std::error_code ec;
    fs::remove(path, ec);
}

struct EnvGuard {
    std::string name;
    bool had_value = false;
    std::string value;

    explicit EnvGuard(const char* key) : name(key) {
        if (const char* current = std::getenv(key)) {
            had_value = true;
            value = current;
        }
    }
    ~EnvGuard() {
        if (had_value)
            chudware::test::setEnvironment(name.c_str(), value.c_str());
        else
            chudware::test::unsetEnvironment(name.c_str());
    }
};

struct CacheSidecarGuard {
    fs::path dir;

    explicit CacheSidecarGuard(const fs::path& path) : dir(path) {
        std::error_code ec;
        fs::remove_all(dir, ec);
        chudware::sim::cache::set_sidecar_dir(dir.string());
        chudware::sim::cache::clear();
    }
    ~CacheSidecarGuard() {
        chudware::sim::cache::clear();
        chudware::sim::cache::set_sidecar_dir("");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
};

TEST_CASE("sim_cache: FEA static HIT / MISS / wall-times") {
    chudware::sim::cache::clear();
    chudware::sim::cache::reset_stats();

    const json study = cube_static_study();

    const auto t0 = std::chrono::steady_clock::now();
    json r1 = chudware::sim::fea_run_static(study);
    const double ms1 = ms_since(t0);
    REQUIRE(r1.value("ok", false));
    CHECK_FALSE(r1.value("cache_hit", true));  // first solve is a miss
    REQUIRE(r1.contains("summary"));
    const double max_disp_1 = r1["summary"]["max_disp_mm"].get<double>();
    const double max_vm_1 = r1["summary"]["max_von_mises_mpa"].get<double>();
    const std::string hash1 = r1.value("input_hash", std::string());
    REQUIRE_FALSE(hash1.empty());

    // Byte-identical result on the second run (HIT).
    const auto t1 = std::chrono::steady_clock::now();
    json r2 = chudware::sim::fea_run_static(study);
    const double ms2 = ms_since(t1);
    REQUIRE(r2.value("ok", false));
    CHECK(r2.value("cache_hit", false));
    CHECK(r2.value("input_hash", std::string()) == hash1);
    CHECK(r2["summary"]["max_disp_mm"].get<double>() ==
          doctest::Approx(max_disp_1).epsilon(1e-15));
    CHECK(r2["summary"]["max_von_mises_mpa"].get<double>() ==
          doctest::Approx(max_vm_1).epsilon(1e-15));
    // Full result dump must match the stored payload (excluding cache tags).
    {
        json a = r1, b = r2;
        a.erase("cache_hit");
        a.erase("cache_reason");
        a.erase("input_hash");
        b.erase("cache_hit");
        b.erase("cache_reason");
        b.erase("input_hash");
        CHECK(a.dump() == b.dump());
    }

    std::printf("[sim_cache] FEA static wall-time miss=%.3f ms  hit=%.3f ms  (hit should be << miss)\n",
                ms1, ms2);
    MESSAGE("FEA static miss ms=", ms1, " hit ms=", ms2);

    // (b) Mutate load -> MISS + different result.
    json study_load = study;
    study_load["loads"][0]["vector"] = {0.0, 0.0, 25000.0};  // half force
    const auto t2 = std::chrono::steady_clock::now();
    json r3 = chudware::sim::fea_run_static(study_load);
    const double ms3 = ms_since(t2);
    REQUIRE(r3.value("ok", false));
    CHECK_FALSE(r3.value("cache_hit", true));
    CHECK(r3.value("input_hash", std::string()) != hash1);
    // Linear static: half force -> ~half disp.
    CHECK(r3["summary"]["max_disp_mm"].get<double>() ==
          doctest::Approx(0.5 * max_disp_1).epsilon(1e-6));
    std::printf("[sim_cache] FEA static load-mutate miss wall-time=%.3f ms\n", ms3);

    // Material mutate -> MISS.
    json study_mat = study;
    study_mat["material"]["E_mpa"] = 100000.0;
    json r4 = chudware::sim::fea_run_static(study_mat);
    REQUIRE(r4.value("ok", false));
    CHECK_FALSE(r4.value("cache_hit", true));
    CHECK(r4.value("input_hash", std::string()) != hash1);

    // Mesh mutate (shift a free node) -> MISS.
    json study_mesh = study;
    study_mesh["points"][3 * 5 + 2] = 1.05;  // nudge node 5 z
    json r5 = chudware::sim::fea_run_static(study_mesh);
    REQUIRE(r5.value("ok", false));
    CHECK_FALSE(r5.value("cache_hit", true));
    CHECK(r5.value("input_hash", std::string()) != hash1);

    auto st = chudware::sim::cache::stats();
    CHECK(st.hits >= 1);
    CHECK(st.misses >= 1);
    CHECK(st.stores >= 1);
}

// A released packet reported summary.max_von_mises_mpa = 109.7 MPa while the .vtu the
// SAME solve wrote peaked at 402.9 MPa. Both are the node-averaged nodal field: the
// summary silently dropped the two element rings around the fixtures (a clamp
// singularity) and the file carried the field without that mask, so the packet's FoS
// and the field disagreed 3.7x with nothing in either artifact to explain it. The
// reported peak is evidence only if the file it ships with can reproduce it.
TEST_CASE("static evidence: the .vtu carries the mask that produced the reported peak") {
    chudware::sim::cache::clear();
    const fs::path vtu = tmp_path("reduction_evidence.vtu");
    std::error_code ec;
    fs::remove(vtu, ec);

    const json study = beam_static_study(vtu.string());
    const json result = chudware::sim::fea_run_static(study);
    REQUIRE_MESSAGE(result.value("ok", false), result.value("error", std::string()));
    REQUIRE(result.value("vtu_path", std::string()) == vtu.string());
    REQUIRE(fs::exists(vtu));

    std::ifstream in(vtu);
    const std::string xml((std::istreambuf_iterator<char>(in)),
                          std::istreambuf_iterator<char>());
    auto array_named = [&xml](const std::string& name) {
        std::vector<double> values;
        const size_t at = xml.find("Name=\"" + name + "\"");
        if (at == std::string::npos) return values;
        const size_t open = xml.find('>', at);
        const size_t close = xml.find("</DataArray>", open);
        if (open == std::string::npos || close == std::string::npos) return values;
        std::istringstream body(xml.substr(open + 1, close - open - 1));
        double v = 0.0;
        while (body >> v) values.push_back(v);
        return values;
    };
    const std::vector<double> vm = array_named("von_mises");
    const std::vector<double> mask = array_named("summary_node");
    const size_t n_nodes = static_cast<size_t>(result.value("n_nodes", 0));
    REQUIRE(n_nodes > 0);
    REQUIRE(vm.size() == n_nodes);
    REQUIRE(mask.size() == n_nodes);

    double masked = 0.0, unmasked = 0.0;
    int excluded = 0;
    for (size_t i = 0; i < vm.size(); ++i) {
        unmasked = std::max(unmasked, vm[i]);
        if (mask[i] != 0.0)
            masked = std::max(masked, vm[i]);
        else
            ++excluded;
    }
    const json summary = result.at("summary");
    // Summary scalars are rounded to 4 decimals for display; 5e-4 IS that rounding
    // step, so the identity below is exact up to how the number is printed.
    CHECK(std::abs(summary.at("max_von_mises_mpa").get<double>() - masked) < 5e-4);
    CHECK(std::abs(summary.at("max_von_mises_raw_mpa").get<double>() - unmasked) < 5e-4);
    CHECK(summary.at("max_von_mises_reduction").get<std::string>() ==
          "node_averaged_excluding_fixture_ring2");
    CHECK(summary.at("summary_nodes_excluded").get<int>() == excluded);
    CHECK(summary.at("summary_nodes_total").get<int>() == static_cast<int>(n_nodes));
    CHECK(excluded > 0);
    // This clamped beam's peak IS in the excluded ring — the disclosure exists for
    // exactly this case, so both the FoS twin and the plain-words warning must fire.
    CHECK(unmasked > masked);
    CHECK(summary.at("min_fos_raw").get<double>() <
          summary.at("min_fos").get<double>());
    REQUIRE(result.contains("warnings"));
    bool warned = false;
    for (const auto& w : result["warnings"])
        warned = warned || w.get<std::string>().find("peak excluded near fixtures") !=
                               std::string::npos;
    CHECK(warned);
    // The named fixture is the one the peak sits next to, not a bare ordinal.
    bool named = false;
    for (const auto& note : result["solver_notes"])
        named = named || note.get<std::string>().find("Ftestfix") != std::string::npos;
    CHECK(named);

    fs::remove(vtu, ec);
}

TEST_CASE("sim_cache: SPICE HIT / invalidate-on-input-change") {
    chudware::sim::cache::clear();
    chudware::sim::cache::reset_stats();

    const json circuit = simple_spice_circuit(1000.0, 1.0);
    const json study = spice_op_study();

    const auto t0 = std::chrono::steady_clock::now();
    json r1 = chudware::sim::spice_simulate(circuit, study);
    const double ms1 = ms_since(t0);
    REQUIRE(r1["convergence"].value("op", false));
    CHECK_FALSE(r1.value("cache_hit", true));
    REQUIRE(r1.contains("op"));
    const double vout1 = r1["op"]["V(OUT)"].get<double>();
    const std::string h1 = r1.value("input_hash", std::string());

    const auto t1 = std::chrono::steady_clock::now();
    json r2 = chudware::sim::spice_simulate(circuit, study);
    const double ms2 = ms_since(t1);
    REQUIRE(r2["convergence"].value("op", false));
    CHECK(r2.value("cache_hit", false));
    CHECK(r2["op"]["V(OUT)"].get<double>() == doctest::Approx(vout1).epsilon(1e-15));
    CHECK(r2.value("input_hash", std::string()) == h1);
    std::printf("[sim_cache] SPICE wall-time miss=%.3f ms  hit=%.3f ms\n", ms1, ms2);
    MESSAGE("SPICE miss ms=", ms1, " hit ms=", ms2);

    // Deck change (series R) -> MISS.
    json circuit2 = simple_spice_circuit(2000.0, 1.0);
    json r3 = chudware::sim::spice_simulate(circuit2, study);
    REQUIRE(r3["convergence"].value("op", false));
    CHECK_FALSE(r3.value("cache_hit", true));
    CHECK(r3.value("input_hash", std::string()) != h1);
    // Voltage divider still 0.5*V but through different R — same Vout for equal divider,
    // so change probe by changing V instead:
    json circuit3 = simple_spice_circuit(1000.0, 2.0);
    json r4 = chudware::sim::spice_simulate(circuit3, study);
    REQUIRE(r4["convergence"].value("op", false));
    CHECK_FALSE(r4.value("cache_hit", true));
    CHECK(r4["op"]["V(OUT)"].get<double>() == doctest::Approx(2.0 * vout1).epsilon(1e-9));
}

TEST_CASE("sim_cache: CFD HIT / invalidate-on-input-change") {
    chudware::sim::cache::clear();
    chudware::sim::cache::reset_stats();

    std::vector<float> pos;
    std::vector<uint32_t> idx;
    unit_cube_tris(pos, idx);
    chudware::sim::FlowParams fp;
    fp.cells = 32;
    fp.n_steps = 500;
    fp.velocity_ms = 0.1;
    fp.nu_fluid = 1.0e-4;

    const auto t0 = std::chrono::steady_clock::now();
    auto r1 = chudware::sim::run_wind_tunnel({pos}, {idx}, fp);
    const double ms1 = ms_since(t0);
    INFO("CFD cache setup error: ", r1.error);
    REQUIRE(r1.ok);
    const double drag1 = r1.drag_n;
    const double lift1 = r1.lift_n;
    const double ref1 = r1.ref_length_mm;

    const auto t1 = std::chrono::steady_clock::now();
    auto r2 = chudware::sim::run_wind_tunnel({pos}, {idx}, fp);
    const double ms2 = ms_since(t1);
    REQUIRE(r2.ok);
    CHECK(r2.drag_n == doctest::Approx(drag1).epsilon(1e-12));
    CHECK(r2.lift_n == doctest::Approx(lift1).epsilon(1e-12));
    CHECK(r2.ref_length_mm == doctest::Approx(ref1).epsilon(1e-12));
    std::printf("[sim_cache] CFD wall-time miss=%.3f ms  hit=%.3f ms\n", ms1, ms2);
    MESSAGE("CFD miss ms=", ms1, " hit ms=", ms2);

    // Input change (velocity) -> MISS (different forces, or at least a recompute path).
    chudware::sim::cache::Stats before = chudware::sim::cache::stats();
    fp.velocity_ms = 0.2;
    auto r3 = chudware::sim::run_wind_tunnel({pos}, {idx}, fp);
    REQUIRE(r3.ok);
    chudware::sim::cache::Stats after = chudware::sim::cache::stats();
    // A new key must have been a miss (stores or misses increased).
    CHECK(after.misses + after.stores > before.misses + before.stores - 1);
    CHECK(after.misses >= before.misses);
}

TEST_CASE("sim_cache: .step+ save->reopen restores study result") {
    chudware::sim::cache::clear();

    // Run a small FEA study, embed the result into a .step+ via the reserved seam.
    json study = cube_static_study();
    json res = chudware::sim::fea_run_static(study);
    REQUIRE(res.value("ok", false));
    const std::string input_hash = res.value("input_hash", std::string());
    REQUIRE_FALSE(input_hash.empty());
    // Strip cache tags for the stored payload (same as process store).
    json payload = res;
    payload.erase("cache_hit");
    payload.erase("cache_reason");
    payload.erase("input_hash");

    json doc = {
        {"schema", "aicad.part/1"},
        {"name", "SimCachePart"},
        {"units", {{"system", "MMGS"}}},
        {"features", json::array()},
        {"studies",
         json::array({
             json{{"id", "s_static"},
                  {"type", "static"},
                  {"material", study["material"]},
                  {"loads", study["loads"]},
                  {"fixtures", study["fixtures"]}},
         })},
    };

    std::map<std::string, std::string> extra;
    // Erase-first discipline (geometry cache style).
    chudware::doc::stepplus_erase_study_results(extra);
    REQUIRE(chudware::doc::stepplus_put_study_result(extra, "s_static", "fea_static", input_hash,
                                                  payload));
    REQUIRE(extra.count(chudware::doc::stepplus_study_result_entry("s_static")) == 1);

    const fs::path path = tmp_path("study_results.step+");
    std::error_code ec;
    fs::remove(path, ec);
    const std::string saved = chudware::doc::stepplus_save(doc, path.string(), extra);
    CHECK(fs::exists(saved));

    chudware::doc::StepPlusDoc loaded = chudware::doc::stepplus_open(saved);
    CHECK(loaded.doc.value("name", std::string()) == "SimCachePart");

    std::string kind_out, hash_out;
    json result_out;
    REQUIRE(chudware::doc::stepplus_get_study_result(loaded.extra, "s_static", kind_out, hash_out,
                                                  result_out));
    CHECK(kind_out == "fea_static");
    CHECK(hash_out == input_hash);
    CHECK(result_out.value("ok", false));
    CHECK(result_out["summary"]["max_disp_mm"].get<double>() ==
          doctest::Approx(payload["summary"]["max_disp_mm"].get<double>()).epsilon(1e-15));
    // Full payload round-trip.
    CHECK(result_out.dump() == payload.dump());

    // Validate-on-open: a mismatched input_hash is the caller's job to reject.
    // Corrupt the engine key by hand-editing the blob -> get returns false.
    {
        std::string& blob = loaded.extra[chudware::doc::stepplus_study_result_entry("s_static")];
        if (blob.size() > 20) blob[12] = static_cast<char>(blob[12] ^ 0x5a);  // corrupt header
        std::string k2, h2;
        json r2;
        CHECK_FALSE(chudware::doc::stepplus_get_study_result(loaded.extra, "s_static", k2, h2, r2));
    }

    // 20 MB cap: oversized payload is refused (honest drop).
    {
        std::map<std::string, std::string> e2;
        json huge = {{"ok", true}, {"blob", std::string(21ull * 1024ull * 1024ull, 'x')}};
        CHECK_FALSE(chudware::doc::stepplus_put_study_result(e2, "big", "fea_static", "deadbeef",
                                                          huge));
        CHECK(e2.count(chudware::doc::stepplus_study_result_entry("big")) == 0);
    }

    fs::remove(path, ec);
    std::printf("[sim_cache] .step+ study result round-trip OK (input_hash=%s)\n",
                input_hash.c_str());
}

TEST_CASE("stepplus rejects excessive compression before extraction") {
    const fs::path path = tmp_path("compression_bomb.step+");
    write_zip(path, {{"meta.json", R"({"container":"aicad.step+/1"})"},
                     {"document.json", R"({"schema":"aicad.part/1"})"},
                     {"bomb.bin", std::string(8 * 1024 * 1024, 'A')}});
    check_stepplus_limit(path, "compression ratio limit");
}

TEST_CASE("stepplus rejects excessive archive entry count before extraction") {
    const fs::path path = tmp_path("entry_count_bomb.step+");
    std::vector<std::pair<std::string, std::string>> entries;
    entries.reserve(4097);
    for (int i = 0; i < 4097; ++i) entries.emplace_back("e/" + std::to_string(i), "x");
    write_zip(path, entries);
    check_stepplus_limit(path, "entry count limit");
}

TEST_CASE("stepplus rejects excessive aggregate expansion before extraction") {
    const fs::path path = tmp_path("aggregate_bomb.step+");
    std::vector<std::pair<std::string, std::string>> entries;
    const std::string incompressible_chunk(1024 * 1024, '\0');
    for (int i = 0; i < 257; ++i) {
        std::string data = incompressible_chunk;
        std::uint32_t state = 0x9e3779b9u ^ static_cast<std::uint32_t>(i);
        for (char& byte : data) {
            state ^= state << 13; state ^= state >> 17; state ^= state << 5;
            byte = static_cast<char>(state & 0xffu);
        }
        entries.emplace_back("payload/" + std::to_string(i), std::move(data));
    }
    write_zip(path, entries);
    check_stepplus_limit(path, "aggregate expanded byte limit");
}

TEST_CASE("stepplus reopens editable data without an oversized optional mesh cache") {
    const fs::path path = tmp_path("oversized_mesh_cache.step+");
    const json doc = {{"schema", "aicad.part/1"}, {"name", "Retained model"},
                      {"features", json::array({{{"id", "f1"}, {"type", "box"},
                                                {"params", {{"length", 10}}}}})}};
    const std::string large(64ull * 1024ull * 1024ull + 1, 'A');
    const std::string future = "future-format user data";
    SUBCASE("legacy reader skips the payload without inflating it") {
        write_zip(path, {{"meta.json", R"({"container":"aicad.step+/1"})"},
                         {"document.json", doc.dump()}, {"cache.aicm", large},
                         {"future.bin", future}});
    }
    SUBCASE("writer omits an inadmissible display cache") {
        chudware::doc::stepplus_save(doc, path.string(),
            {{"cache.aicm", std::string(256ull * 1024ull * 1024ull + 1, 'A')}, {"future.bin", future}});
    }
    const auto loaded = chudware::doc::stepplus_open(path.string());
    CHECK(loaded.doc == doc);
    CHECK(loaded.extra.at("future.bin") == future);
    CHECK(loaded.extra.count("cache.aicm") == 0);
    std::error_code ec;
    fs::remove(path, ec);
}

#if !defined(_WIN32)
TEST_CASE("stepplus ordinary save never follows a predictable staging alias") {
    const fs::path dir = tmp_path("staging_alias");
    std::error_code ec;
    fs::remove_all(dir, ec);
    REQUIRE(fs::create_directories(dir));
    const fs::path path = dir / "part.step+";
    const fs::path sentinel = dir / "sibling-sentinel";
    { std::ofstream(sentinel, std::ios::binary) << "untouched sibling fixture"; }
    fs::create_symlink(sentinel, fs::path(path.string() + ".tmp"));
    const json doc = {{"schema", "aicad.part/1"}, {"name", "Staging"}, {"features", json::array()}};
    chudware::doc::stepplus_save(doc, path.string(), {});
    std::ifstream in(sentinel, std::ios::binary);
    CHECK(std::string(std::istreambuf_iterator<char>(in), {}) == "untouched sibling fixture");
    CHECK(chudware::doc::stepplus_open(path.string()).doc == doc);
    std::size_t entries = 0;
    for (const auto& entry : fs::directory_iterator(dir)) {
        (void)entry;
        ++entries;
    }
    CHECK(entries == 3);  // published archive, sentinel and the untouched alias only
    fs::remove_all(dir, ec);
}
#endif

TEST_CASE("native metadata inspection extracts only the entry it admitted") {
    const fs::path path = fs::absolute(tmp_path("uppercase_metadata.step+"));
    write_zip(path, {{"meta.json", R"({"container":"aicad.step+/1"})"},
                     {"DOCUMENT.JSON", std::string(65536, ' ') +
                                       R"({"schema":"aicad.part/1","features":[]})"}},
              MZ_BEST_COMPRESSION);
    const json inspected = chudware::doc::native_file_operations(
        {{"action", "inspect"}, {"paths", json::array({path.string()})}});
    REQUIRE(inspected.at("files").size() == 1);
    CHECK(inspected.at("files")[0].value("ok", true) == false);
    std::error_code ec;
    fs::remove(path, ec);
}

TEST_CASE("stepplus retains large STORED mesh caches within the archive budget") {
    const fs::path path = tmp_path("large_stored_mesh_cache.step+");
    const json doc = {{"schema", "aicad.part/1"}, {"name", "Cached model"}};
    std::map<std::string, std::string> extra;
    extra.emplace("cache.aicm", std::string(64ull * 1024ull * 1024ull + 1, 'A'));
    chudware::doc::stepplus_save(doc, path.string(), extra);
    const auto loaded = chudware::doc::stepplus_open(path.string());
    CHECK(loaded.doc == doc);
    CHECK(loaded.extra.at("cache.aicm") == extra.at("cache.aicm"));
    std::error_code ec;
    fs::remove(path, ec);
}

TEST_CASE("stepplus reserves archive capacity for user data before optional meshes") {
    const fs::path path = tmp_path("mesh_budget_priority.step+");
    const json doc = {{"schema", "aicad.part/1"}, {"name", "User data wins"}};
    std::vector<std::pair<std::string, std::string>> entries{
        {"meta.json", R"({"container":"aicad.step+/1"})"}, {"document.json", doc.dump()}};
    for (int i = 0; i < 4; ++i)
        entries.emplace_back("future/" + std::to_string(i),
                             std::string(48ull * 1024ull * 1024ull, static_cast<char>('a' + i)));
    entries.emplace_back("cache.aicm", std::string(64ull * 1024ull * 1024ull, 'm'));
    write_zip(path, entries, MZ_NO_COMPRESSION);
    const auto loaded = chudware::doc::stepplus_open(path.string());
    CHECK(loaded.doc == doc);
    CHECK(loaded.extra.count("cache.aicm") == 0);
    for (int i = 0; i < 4; ++i)
        CHECK(loaded.extra.at("future/" + std::to_string(i)) == entries[i + 2].second);
    std::error_code ec;
    fs::remove(path, ec);
}

TEST_CASE("stepplus mesh-cache exception does not admit oversized source or unknown data") {
    const fs::path path = tmp_path("oversized_required_entry.step+");
    const std::string large(64ull * 1024ull * 1024ull + 1, 'A');
    std::string entry;
    SUBCASE("source document") { entry = "document.json"; }
    SUBCASE("unknown user entry") { entry = "future.bin"; }
    write_zip(path, {{"meta.json", R"({"container":"aicad.step+/1"})"}, {entry, large}});
    check_stepplus_limit(path, "per-entry expanded byte limit");
}

TEST_CASE("stepplus opens its own save whose geometry.step snapshot exceeds the entry cap") {
    // The interop snapshot is never inflated by doc_open; bounding it made a dense lofted
    // part's own save unopenable ("per-entry expanded byte limit").
    const fs::path path = tmp_path("large_geometry_snapshot.step+");
    const json doc = {{"schema", "aicad.part/1"}, {"name", "Dense loft"}};
    chudware::doc::stepplus_save(doc, path.string(),
        {{"geometry.step", std::string(64ull * 1024ull * 1024ull + 1, 'S')}});
    const auto loaded = chudware::doc::stepplus_open(path.string());
    CHECK(loaded.doc == doc);
    CHECK(loaded.extra.count("geometry.step") == 0);
    std::error_code ec;
    fs::remove(path, ec);
}

namespace {
struct NativeFileFixture {
    fs::path root = tmp_path("native_files_" + std::to_string(
        std::chrono::steady_clock::now().time_since_epoch().count()));
    NativeFileFixture() { REQUIRE(fs::create_directory(root)); }
    ~NativeFileFixture() {
        std::error_code ec;
        fs::remove_all(root, ec);
    }
};

std::string native_file_bytes(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    REQUIRE(input.good());
    return std::string(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
}

std::map<std::string, std::string> native_zip_payloads(const fs::path& path) {
    mz_zip_archive zip{};
    REQUIRE(mz_zip_reader_init_file(&zip, path.string().c_str(), 0));
    std::map<std::string, std::string> entries;
    for (mz_uint i = 0; i < mz_zip_reader_get_num_files(&zip); ++i) {
        mz_zip_archive_file_stat stat{};
        REQUIRE(mz_zip_reader_file_stat(&zip, i, &stat));
        std::string bytes(static_cast<std::size_t>(stat.m_uncomp_size), '\0');
        REQUIRE(mz_zip_reader_extract_to_mem(&zip, i, bytes.data(), bytes.size(), 0));
        entries.emplace(stat.m_filename, std::move(bytes));
    }
    REQUIRE(mz_zip_reader_end(&zip));
    return entries;
}
}  // namespace

TEST_CASE("stepplus native relocation preserves models and rebases mapped and retained references") {
    using chudware::doc::native_file_operations;
    NativeFileFixture fixture;
    const auto old_dir = fixture.root / "old";
    const auto assembly_path = old_dir / "assembly.step+";
    const auto part_path = old_dir / "part.step+";
    const auto new_assembly = fixture.root / "organized" / "assemblies" / "Main.step+";
    const auto new_part = fixture.root / "organized" / "parts" / "Bearing.step+";
    fs::create_directories(old_dir);
    const auto mapped_source = fixture.root / "vendor" / "catalog.step";
    const auto mapped_target = fixture.root / "organized" / "vendor" / "Catalog.step";
    fs::create_directories(mapped_target.parent_path());
    { std::ofstream vendor(mapped_target); vendor << "opaque neutral dependency"; }
    bool absolute_sources = false;
    SUBCASE("originally relative mapped sources") {}
    SUBCASE("originally absolute mapped sources") { absolute_sources = true; }
    fs::path referenced_part_path = part_path;
    SUBCASE("absolute mapped component source uses a directory alias") {
        absolute_sources = true;
        const auto alias = fixture.root / "OLD";
        if (!fs::exists(alias))
            REQUIRE_NOTHROW(fs::create_directory_symlink(old_dir, alias));
        REQUIRE(fs::equivalent(old_dir, alias));
        referenced_part_path = alias / part_path.filename();
    }
    const json pose = json::array({1.0, 0.0, 0.0, 12.123456789012345,
                                  0.0, 1.0, 0.0, -3.25, 0.0, 0.0, 1.0, 9.75,
                                  0.0, 0.0, 0.0, 1.0});
    const json assembly{
        {"schema", "aicad.asm/1"}, {"id", "stable-assembly"}, {"name", "Old assembly"},
        {"components", json::array({
            {{"id", "k1"}, {"name", "Old bearing"},
             {"source", {{"path", absolute_sources ? referenced_part_path.string() : "part.step+"}}},
             {"transform", pose}, {"fixed", true}},
            {{"id", "k2"}, {"name", "Retained vendor"}, {"source", {{"path", "../vendor/retained.step"}}},
             {"transform", pose}, {"fixed", false}}})},
        {"mates", json::array({
            {{"id", "m1"}, {"type", "angle"}, {"value", 0.2718281828459045},
             {"reference_frames", {{"a", pose}, {"b", pose}}}}
        })}};
    const json part{
        {"schema", "aicad.part/1"}, {"id", "stable-part"}, {"name", "Old part"},
        {"meta", {{"provenance", {{"source_path", "/historical/unavailable.step+"},
                                 {"snapshot", {{"path", R"(C:\old\assembly.step+)"}}}}}}},
        {"features", json::array({
            {{"id", "f1"}, {"type", "import"}, {"name", "Do not rename this expression target"},
             {"params", {{"source_path", "../vendor/retained.step"}, {"format", "step"},
                         {"embedded_brep", std::string("opaque\0model", 12)}}}},
            {{"id", "f2"}, {"type", "import"}, {"name", "Historical URI"},
             {"params", {{"path", "https://supplier.invalid/original.step"},
                         {"source_path", absolute_sources ? mapped_source.string() : "../vendor/catalog.step"},
                         {"embedded_brep", "a second opaque model"}, {"format", "step"}}}},
            {{"id", "f3"}, {"type", "sweep"}, {"name", "Geometric path is not a filename"},
             {"params", {{"mode", "new"}}}, {"refs", {{"path", {{"feature", "curve1"}}}}}}})},
        {"materials", {{"f1#1", {{"name", "Steel"}, {"density", 7850.0}}}}},
        {"face_finishes", {{"F123", {{"fingerprint", "exact-face"}, {"color", {0.2, 0.3, 0.4}}}}}}};
    const json meta{{"container", "aicad.step+/1"}, {"created", "original timestamp"},
                    {"future_meta", {{"preserve", true}}}, {"name", "Old assembly"}};
    const std::string geometry("unparsed STEP\0bytes\n", 19);
    const std::string unknown(1024 * 1024, 'Q');
    write_zip(assembly_path, {{"meta.json", meta.dump()}, {"document.json", assembly.dump()},
                             {"geometry.step", geometry}, {"future/", ""}, {"future/payload.bin", unknown},
                             {"thumbnail.png", "original thumbnail"}, {"cache.brep", "obsolete BREP"},
                             {"cache.aicm", "obsolete display"}, {"cache.json", R"({"solved":true})"}},
              MZ_NO_COMPRESSION);
    write_zip(part_path, {{"meta.json", meta.dump()}, {"document.json", part.dump()},
                         {"geometry.step", ""}, {"future.bin", std::string("\0\xff", 2)}},
              MZ_NO_COMPRESSION);
    const auto original_assembly = native_file_bytes(assembly_path);
    const auto original_part = native_file_bytes(part_path);
    const auto inspected = native_file_operations({{"action", "inspect"},
        {"paths", json::array({assembly_path.string(), part_path.string()})}});
    REQUIRE_MESSAGE(inspected["ok"] == true, inspected.dump());
    CHECK(inspected["files"][0]["components"][0]["transform"] == pose);
    CHECK(inspected["files"][1]["dependencies"][0]["required"] == false);
    CHECK(inspected["files"][1]["dependencies"][0]["exists"] == false);
    CHECK(inspected["files"][1]["dependencies"][2]["format"] == "uri");
    CHECK(inspected["files"][1]["dependencies"][2]["rewrite"] == false);
    for (std::size_t i = 3; i < inspected["files"][1]["dependencies"].size(); ++i) {
        CHECK(inspected["files"][1]["dependencies"][i]["required"] == false);
        CHECK_FALSE(inspected["files"][1]["dependencies"][i].value("rewrite", true));
    }
    const auto relocated = native_file_operations({
        {"action", "relocate"},
        {"entries", json::array({
            {{"path", assembly_path.string()}, {"destination", new_assembly.string()}, {"name", "Main"},
             {"component_names", {{"k1", "Bearing"}}}},
            {{"path", part_path.string()}, {"destination", new_part.string()}, {"name", "Bearing"}}})},
        {"path_map", {{assembly_path.string(), new_assembly.string()}, {part_path.string(), new_part.string()},
                      {mapped_source.string(), mapped_target.string()}}}});
    REQUIRE_MESSAGE(relocated["ok"] == true, relocated.dump());
    CHECK(native_file_bytes(assembly_path) == original_assembly);
    CHECK(native_file_bytes(part_path) == original_part);
    const auto assembly_entries = native_zip_payloads(new_assembly);
    const auto part_entries = native_zip_payloads(new_part);
    json expected_assembly = assembly;
    expected_assembly["name"] = "Main";
    expected_assembly["components"][0]["name"] = "Bearing";
    expected_assembly["components"][0]["source"]["path"] = "../parts/Bearing.step+";
    expected_assembly["components"][1]["source"]["path"] = "../../vendor/retained.step";
    json expected_part = part;
    expected_part["name"] = "Bearing";
    expected_part["features"][0]["params"]["source_path"] = "../../vendor/retained.step";
    expected_part["features"][1]["params"]["source_path"] = "../vendor/Catalog.step";
    CHECK(json::parse(assembly_entries.at("document.json")) == expected_assembly);
    CHECK(json::parse(part_entries.at("document.json")) == expected_part);
    CHECK(assembly_entries.at("geometry.step") == geometry);
    CHECK(part_entries.at("geometry.step") == "");
    CHECK(assembly_entries.at("future/payload.bin") == unknown);
    CHECK(assembly_entries.at("future/") == "");
    CHECK(part_entries.at("future.bin") == std::string("\0\xff", 2));
    CHECK(assembly_entries.at("thumbnail.png") == "original thumbnail");
    CHECK(part_entries.count("thumbnail.png") == 0);
    CHECK(assembly_entries.count("cache.brep") == 0);
    CHECK(assembly_entries.count("cache.aicm") == 0);
    CHECK(assembly_entries.count("cache.json") == 0);
    json expected_meta = meta;
    expected_meta["name"] = "Main";
    CHECK(json::parse(assembly_entries.at("meta.json")) == expected_meta);
    const auto after = native_file_operations({{"action", "inspect"},
        {"paths", json::array({new_assembly.string(), new_part.string()})}});
    REQUIRE_MESSAGE(after["ok"] == true, after.dump());
    for (int i = 0; i < 2; ++i) {
        CHECK(after["files"][i]["immutable_document_hash"] == inspected["files"][i]["immutable_document_hash"]);
        CHECK(after["files"][i]["document_id"] == inspected["files"][i]["document_id"]);
    }
    // A STORED highly-compressible unknown entry remains admissible to normal open.
    CHECK(chudware::doc::stepplus_open(new_assembly.string()).extra.at("future/payload.bin") == unknown);
}

TEST_CASE("stepplus relocation preserves Unicode paths despite unrelated Unicode siblings") {
    using namespace chudware::doc;
    using native_policy::path_utf8;
    NativeFileFixture fixture;
    const auto source = fixture.root / fs::u8path(u8"原本-日本.step+");
    const auto dependency = fixture.root / fs::u8path(u8"部品-λ.step+");
    const auto destination = fixture.root / "organized" / fs::u8path(u8"組立-é.step+");
    REQUIRE(fs::create_directory(fixture.root / fs::u8path(u8"無関係-資料")));
#if defined(_WIN32)
    std::wstring unrelated = L"unrelated-invalid-utf16-";
    unrelated.push_back(static_cast<wchar_t>(0xd800));
    REQUIRE(fs::create_directory(fixture.root / unrelated));
#endif
    const json part{{"schema", "aicad.part/1"}, {"id", "unicode-part"},
                    {"name", "Dependency"}, {"features", json::array()}};
    json assembly{{"schema", "aicad.asm/1"}, {"id", "unicode-assembly"},
        {"name", "Before overwrite"},
        {"components", json::array({{{"id", "p1"},
            {"source", {{"path", path_utf8(dependency.filename())}}},
            {"transform", json::array({1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1})},
            {"fixed", true}}})}};
    stepplus_save(part, path_utf8(dependency), {});
    // Exercise Win32 handle-based publication with mixed native/forward separators.
    const auto mixed_path = path_utf8(source.parent_path()) + "/" + path_utf8(source.filename());
    stepplus_save(assembly, mixed_path, {});
    REQUIRE(fs::is_regular_file(source));
    assembly["name"] = "After overwrite";
    stepplus_save(assembly, mixed_path, {});
    CHECK(stepplus_open(path_utf8(source)).doc == assembly);
    const auto source_bytes = native_file_bytes(source);
    const auto inspected = native_file_operations({{"action", "inspect"},
        {"paths", json::array({path_utf8(source)})}});
    REQUIRE_MESSAGE(inspected.value("ok", false), inspected.dump());
    const auto relocated = native_file_operations({{"action", "relocate"},
        {"entries", json::array({{{"path", path_utf8(source)},
                                  {"destination", path_utf8(destination)}}})}});
    REQUIRE_MESSAGE(relocated.value("ok", false), relocated.dump());
    REQUIRE(fs::is_regular_file(destination));
    const auto reopened = stepplus_open(path_utf8(destination));
    assembly["components"][0]["source"]["path"] = "../" + path_utf8(dependency.filename());
    CHECK(reopened.doc == assembly);
    CHECK(stepplus_open(path_utf8(dependency)).doc == part);
    CHECK(native_file_bytes(source) == source_bytes);
}

TEST_CASE("stepplus native relocation accepts an existing directory's alternate identity spelling") {
    using chudware::doc::native_file_operations;
    NativeFileFixture fixture;
    const auto source = fixture.root / "source.step+";
    const auto parent = fixture.root / "models";
    const auto alias = fixture.root / "MODELS";
    REQUIRE(fs::create_directory(parent));
    // On a case-sensitive volume, an alias replays the two spellings of one
    // Windows/macOS directory. A distinct directory still genuinely collides.
    if (!fs::exists(alias))
        REQUIRE_NOTHROW(fs::create_directory_symlink(parent, alias));
    REQUIRE(fs::equivalent(parent, alias));
    const json doc{{"schema", "aicad.part/1"}, {"id", "same-parent"},
                   {"name", "Preserved"}, {"features", json::array()}};
    chudware::doc::stepplus_save(doc, source.string(), {});
    const auto bytes = native_file_bytes(source);
    const auto result = native_file_operations({{"action", "relocate"},
        {"entries", json::array({{{"path", source.string()},
                                  {"destination", (alias / "copy.step+").string()}}})}});
    REQUIRE_MESSAGE(result.value("ok", false), result.dump());
    CHECK(chudware::doc::stepplus_open((parent / "copy.step+").string()).doc == doc);
    CHECK(native_file_bytes(source) == bytes);
}

TEST_CASE("stepplus native relocation refuses foreign or unqualified dependency roots") {
    using chudware::doc::native_file_operations;
    NativeFileFixture fixture;
    const auto source = fixture.root / "assembly.step+";
    const auto destination = fixture.root / "copy.step+";
#if defined(_WIN32)
    const std::string dependency = R"(C:drive-relative.step+)";
#else
    const std::string dependency = R"(C:\foreign\part.step+)";
#endif
    const json doc{{"schema", "aicad.asm/1"}, {"id", "unsupported-path"},
        {"components", json::array({{{"id", "p1"},
                                      {"source", {{"path", dependency}}}}})}};
    chudware::doc::stepplus_save(doc, source.string(), {});
    const auto bytes = native_file_bytes(source);
    const auto inspected = native_file_operations({{"action", "inspect"},
        {"paths", json::array({source.string()})}});
    REQUIRE_MESSAGE(inspected.value("ok", false), inspected.dump());
    const auto& reference = inspected.at("files").at(0).at("dependencies").at(0);
    CHECK(reference.at("supported") == false);
    CHECK_FALSE(reference.contains("resolved_path"));
    const auto relocated = native_file_operations({{"action", "relocate"},
        {"entries", json::array({{{"path", source.string()}, {"destination", destination.string()}}})}});
    CHECK_FALSE(relocated.value("ok", true));
    CHECK_FALSE(fs::exists(destination));
    CHECK(native_file_bytes(source) == bytes);
}

TEST_CASE("stepplus native preflight refuses missing and colliding targets before any writes") {
    using chudware::doc::native_file_operations;
    NativeFileFixture fixture;
    const auto source = fixture.root / "source.step+";
    const auto other = fixture.root / "other.step+";
    const auto destination = fixture.root / "new" / "copy.step+";
    const json doc{{"schema", "aicad.part/1"}, {"id", "preserve-id"}, {"name", "Original"},
                   {"features", json::array()}};
    chudware::doc::stepplus_save(doc, source.string(), {});
    chudware::doc::stepplus_save(doc, other.string(), {});
    const auto source_bytes = native_file_bytes(source);
    const auto other_bytes = native_file_bytes(other);
    json entries = json::array({{{"path", source.string()}, {"destination", destination.string()}}});
    json path_map = json::object();
    SUBCASE("later missing source") {
        entries.push_back({{"path", (fixture.root / "missing.step+").string()},
                           {"destination", (fixture.root / "missing-copy.step+").string()}});
    }
    SUBCASE("later destination would overwrite source") {
        entries.push_back({{"path", other.string()}, {"destination", source.string()}});
    }
    SUBCASE("missing destination field") {
        entries.push_back({{"path", other.string()}});
    }
    SUBCASE("case-colliding destinations") {
        entries.push_back({{"path", other.string()},
                           {"destination", (fixture.root / "new" / "COPY.step+").string()}});
    }
    SUBCASE("case-colliding planned directory names") {
        entries.push_back({{"path", other.string()},
                           {"destination", (fixture.root / "NEW" / "other-copy.step+").string()}});
    }
    SUBCASE("planned file is another destination parent") {
        entries.push_back({{"path", other.string()},
                           {"destination", (destination / "nested.step+").string()}});
    }
    SUBCASE("dangling symlink is an occupied destination") {
        const auto link = fixture.root / "dangling.step+";
        std::error_code ec;
        fs::create_symlink(fixture.root / "nonexistent.step+", link, ec);
        if (ec) return;  // symlink creation may require privileges on Windows
        entries[0]["destination"] = link.string();
    }
    SUBCASE("case-colliding existing destination") {
        entries[0]["destination"] = (fixture.root / "SOURCE.step+").string();
    }
    SUBCASE("case-colliding existing destination directory") {
        REQUIRE(fs::create_directory(fixture.root / "Models"));
        entries[0]["destination"] = (fixture.root / "models" / "copy.step+").string();
        // On a case-insensitive volume these ancestors are one directory;
        // the leaf is what must not be overwritten.
        if (fs::exists(fixture.root / "models")) {
            std::ofstream occupied(fixture.root / "Models" / "copy.step+");
            occupied << "keep";
        }
    }
    SUBCASE("path map disagrees with plan") {
        path_map[source.string()] = (fixture.root / "wrong.step+").string();
    }
    SUBCASE("unknown component id") {
        entries[0]["component_names"] = {{"does-not-exist", "Typo"}};
    }
    const auto result = native_file_operations({{"action", "relocate"}, {"entries", entries},
                                                 {"path_map", path_map}});
    CHECK_FALSE(result.value("ok", true));
    REQUIRE_FALSE(result["failures"].empty());
    CHECK_FALSE(fs::exists(destination));
    CHECK_FALSE(fs::exists(destination.parent_path()));
    CHECK(native_file_bytes(source) == source_bytes);
    CHECK(native_file_bytes(other) == other_bytes);
    for (const auto& file : result["files"]) CHECK(file["status"] == "not_written");
}

TEST_CASE("stepplus native inspection skips oversized cache expansion and exposes unsupported sources") {
    using chudware::doc::native_file_operations;
    NativeFileFixture fixture;
    const auto source = fixture.root / "source.step+";
    const auto destination = fixture.root / "copy.step+";
    const json doc{{"schema", "aicad.asm/1"}, {"name", "Metadata only"},
                   {"components", json::array({
                       {{"id", "k1"}, {"name", "Opaque"},
                        {"source", {{"embedded", "unrecognized snapshot format"}}}}})}};
    write_zip(source, {{"meta.json", R"({"container":"aicad.step+/1"})"},
                       {"document.json", doc.dump()}, {"cache.brep", std::string(8 * 1024 * 1024, 'B')}});
    const auto inspected = native_file_operations({{"action", "inspect"}, {"paths", {source.string()}}});
    REQUIRE_MESSAGE(inspected["ok"] == true, inspected.dump());
    CHECK(inspected["files"][0]["dependencies"][0]["supported"] == false);
    const auto result = native_file_operations({{"action", "relocate"},
        {"entries", json::array({{{"path", source.string()}, {"destination", destination.string()}}})}});
    CHECK_FALSE(result.value("ok", true));
    CHECK_FALSE(fs::exists(destination));
    CHECK(chudware::doc::native_file_operations({{"action", "inspect"},
          {"paths", {source.string()}}})["files"][0]["document_hash"] ==
          inspected["files"][0]["document_hash"]);
}

TEST_CASE("sim_cache: hash is stable and kind-scoped") {
    json s = cube_static_study();
    const std::string a = chudware::sim::cache::hash_fea_study(Kind::FeaStatic, s);
    const std::string b = chudware::sim::cache::hash_fea_study(Kind::FeaStatic, s);
    CHECK(a == b);
    const std::string modal = chudware::sim::cache::hash_fea_study(Kind::FeaModal, s);
    CHECK(modal != a);  // kinds never collide
    s["material"]["E_mpa"] = 100000.0;
    CHECK(chudware::sim::cache::hash_fea_study(Kind::FeaStatic, s) != a);
}

TEST_CASE("sim_cache: selected solver and iterative limits cannot reuse another result") {
    EnvGuard backend("CHUDWARE_FEA_SOLVER");
    EnvGuard limit("CHUDWARE_FEA_ICCG_MAX_ITERATIONS");
    CacheSidecarGuard cache(tmp_path("solver_policy"));
    const auto study = cube_static_study();
    chudware::test::setEnvironment("CHUDWARE_FEA_SOLVER", "ldlt");
    const auto direct = chudware::sim::fea_run_static(study);
    REQUIRE_MESSAGE(direct.value("ok", false), direct.dump());

    chudware::test::setEnvironment("CHUDWARE_FEA_SOLVER", "iccg");
    chudware::test::setEnvironment("CHUDWARE_FEA_ICCG_MAX_ITERATIONS", "10000");
    const auto iterative = chudware::sim::fea_run_static(study);
    REQUIRE_MESSAGE(iterative.value("ok", false), iterative.dump());
    CHECK_FALSE(iterative.value("cache_hit", true));
    CHECK(chudware::sim::fea_run_static(study).value("cache_hit", false));

    // A cached successful solve must not hide an invalid newly selected limit.
    chudware::test::setEnvironment("CHUDWARE_FEA_ICCG_MAX_ITERATIONS", "0");
    const auto invalid = chudware::sim::fea_run_static(study);
    CHECK_FALSE(invalid.value("ok", true));
    chudware::test::setEnvironment("CHUDWARE_FEA_SOLVER", "ldlt");
    CHECK(chudware::sim::fea_run_static(study).value("cache_hit", false));
}

TEST_CASE("sim_cache: exact finite hashes never alias rounded inputs") {
    json a = {{"mesh_fineness", 1.0}, {"solver", {{"tol", 1.0}}}};
    json b = a;
    b["solver"]["tol"] = std::nextafter(1.0, 2.0);
    const std::string ha = chudware::sim::cache::hash_inputs(Kind::FeaStatic, a);
    const std::string hb = chudware::sim::cache::hash_inputs(Kind::FeaStatic, b);
    REQUIRE_FALSE(ha.empty());
    CHECK(ha != hb);

    json nonfinite = a;
    nonfinite["solver"]["tol"] = std::numeric_limits<double>::quiet_NaN();
    CHECK(chudware::sim::cache::hash_inputs(Kind::FeaStatic, nonfinite).empty());
    CHECK_FALSE(chudware::sim::cache::store(Kind::FeaStatic, ha, nonfinite));

    json missing_geometry = {{"step_path", "/definitely/not/a/chudware/model.step"}};
    CHECK(chudware::sim::cache::hash_fea_study(Kind::FeaStatic, missing_geometry).empty());
    CHECK(chudware::sim::cache::hash_fea_study(Kind::Cfd, a).empty());
}

TEST_CASE("sim_cache: kind isolation and corrupted sidecars fail closed") {
    namespace cache = chudware::sim::cache;
    CacheSidecarGuard sidecars(tmp_path("sidecars"));
    const fs::path& dir = sidecars.dir;

    const std::string hash = cache::hash_inputs(Kind::FeaStatic, json{{"case", 7}});
    REQUIRE_FALSE(hash.empty());
    REQUIRE(cache::store(Kind::FeaStatic, hash, json{{"value", 11}}));
    REQUIRE(cache::store(Kind::FeaModal, hash, json{{"value", 22}}));
    CHECK(cache::lookup(Kind::FeaStatic, hash).result.value("value", 0) == 11);
    CHECK(cache::lookup(Kind::FeaModal, hash).result.value("value", 0) == 22);

    cache::clear();
    auto disk_hit = cache::lookup(Kind::FeaStatic, hash);
    REQUIRE(disk_hit.hit);
    CHECK(disk_hit.result.value("value", 0) == 11);

    const fs::path sidecar = dir / ("fea_static_" + hash + ".json");
    json envelope;
    {
        std::ifstream in(sidecar, std::ios::binary);
        REQUIRE(in.good());
        in >> envelope;
    }
    envelope["result"]["value"] = 99;  // valid JSON, invalid bound checksum
    {
        std::ofstream out(sidecar, std::ios::binary | std::ios::trunc);
        out << envelope.dump();
    }
    cache::clear();
    auto corrupt = cache::lookup(Kind::FeaStatic, hash);
    CHECK_FALSE(corrupt.hit);
    CHECK(corrupt.reason == "corrupt");

    {
        std::ofstream out(sidecar, std::ios::binary | std::ios::trunc);
        out << "{\"engine\":";
    }
    cache::clear();
    auto partial = cache::lookup(Kind::FeaStatic, hash);
    CHECK_FALSE(partial.hit);
    CHECK(partial.reason == "corrupt");

    cache::clear();
}

TEST_CASE("sim_cache: result blobs bind every body byte and publish atomically") {
    namespace cache = chudware::sim::cache;
    const json payload = {{"ok", true}, {"value", 12345}};
    std::string blob = cache::encode_result_blob(Kind::FeaStatic, "deadbeef", payload);
    REQUIRE_FALSE(blob.empty());

    const std::string original_blob = blob;
    Kind kind = Kind::Em;
    std::string hash = "unchanged";
    json decoded = {{"sentinel", true}};
    REQUIRE(cache::decode_result_blob(blob, kind, hash, decoded));
    CHECK(kind == Kind::FeaStatic);
    CHECK(hash == "deadbeef");
    CHECK(decoded == payload);

    const size_t value_pos = blob.rfind("12345");
    REQUIRE(value_pos != std::string::npos);
    blob[value_pos] = '9';  // body remains valid JSON; checksum must catch it
    kind = Kind::Em;
    hash = "unchanged";
    const json sentinel = {{"sentinel", true}};
    decoded = sentinel;
    CHECK_FALSE(cache::decode_result_blob(blob, kind, hash, decoded));
    CHECK(kind == Kind::Em);
    CHECK(hash == "unchanged");
    CHECK(decoded == sentinel);
    blob = original_blob;
    blob.resize(blob.size() - 1);
    CHECK_FALSE(cache::decode_result_blob(blob, kind, hash, decoded));

    json nonfinite = {{"value", std::numeric_limits<double>::infinity()}};
    CHECK(cache::encode_result_blob(Kind::FeaStatic, "deadbeef", nonfinite).empty());
}

TEST_CASE("sim_cancel: reused job ids cannot unregister the newer generation") {
    using namespace chudware::sim;
    auto old_token = sim_job_register("sim-cache-cancel-generation");
    REQUIRE(old_token);
    auto new_token = sim_job_register("sim-cache-cancel-generation");
    REQUIRE(new_token);
    CHECK(old_token->load(std::memory_order_acquire));
    CHECK_FALSE(new_token->load(std::memory_order_acquire));

    sim_job_unregister("sim-cache-cancel-generation", old_token);
    auto cancelled = sim_cancel("sim-cache-cancel-generation");
    CHECK(cancelled.value("cancelled", false));
    CHECK(new_token->load(std::memory_order_acquire));

    sim_job_unregister("sim-cache-cancel-generation", new_token);
    auto finished = sim_cancel("sim-cache-cancel-generation");
    CHECK_FALSE(finished.value("cancelled", true));
}

TEST_CASE("numpy_rng: PCG64 and normal streams match NumPy reference vectors") {
    chudware::sim::NumpyGenerator raw(0);
    CHECK(raw.next_uint64() == 11749869230777074271ULL);

    chudware::sim::NumpyGenerator normal(0);
    const double expected[] = {0.1257302210933933, -0.1321048632913019,
                               0.6404226504432821, 0.10490011715303971,
                               -0.535669373161111};
    for (double value : expected)
        CHECK(normal.standard_normal() == doctest::Approx(value).epsilon(1e-15));
}

TEST_CASE("solver_threads: configured requests remain within the hard host cap") {
    EnvGuard guard("CHUDWARE_FEA_THREADS");
    chudware::test::setEnvironment("CHUDWARE_FEA_THREADS", "999999");
    const int configured = chudware::sim::governed_solver_threads();
    CHECK(configured >= 1);
    CHECK(configured <= 8);

    chudware::test::setEnvironment("CHUDWARE_FEA_THREADS",
                                    "999999999999999999999999999999");
    const int overflowed = chudware::sim::governed_solver_threads();
    CHECK(overflowed >= 1);
    CHECK(overflowed <= 8);
}

#include "doc/native_policy.h"
#include "piping/solve.h"

namespace {
struct NumericalRootPolicyGuard {
    std::string root;
    std::string state;
    bool configured = false;
    explicit NumericalRootPolicyGuard(const std::string& workspace) {
        auto& config = chudware::doc::native_policy::configuration();
        {
            std::lock_guard<std::mutex> lock(config.mutex);
            root = config.root;
            state = config.state;
            configured = config.configured;
        }
        chudware::doc::native_policy::configure(workspace, "");
    }
    ~NumericalRootPolicyGuard() {
        auto& config = chudware::doc::native_policy::configuration();
        std::lock_guard<std::mutex> lock(config.mutex);
        config.root = root;
        config.state = state;
        config.configured = configured;
    }
};
}

TEST_CASE("pump curve rooted file sources refuse escapes while inline CSV remains usable") {
    NativeFileFixture fixture;
    const fs::path work = fixture.root / "work";
    REQUIRE(fs::create_directory(work));
    NumericalRootPolicyGuard policy(work.string());
    const std::string csv = "flow,head\n0,20\n10,10\n";
    const auto curve_file = work / "curve.csv";
    const auto outside = fixture.root / "outside.csv";
    for (const auto& path : {curve_file, outside}) {
        std::ofstream out(path);
        REQUIRE(out.good());
        out << csv;
    }
    const auto inline_curve = chudware::piping::load_pump_curve(csv);
    REQUIRE(inline_curve.value("ok", false));
    const auto file_curve = chudware::piping::load_pump_curve("curve.csv");
    REQUIRE(file_curve.value("ok", false));
    CHECK(file_curve == inline_curve);

    // A CSV header that also names a real file is still text, not a file source.
    {
        std::ofstream out(work / "flow,head");
        REQUIRE(out.good());
        out << csv;
    }
    const auto header_only = chudware::piping::load_pump_curve("flow,head");
    CHECK_FALSE(header_only.value("ok", true));
    CHECK(header_only.value("reason", "") == "pump curve needs >= 2 valid points");

    std::vector<std::string> refused{
        "../outside.csv", "nested/../../outside.csv", outside.string(), curve_file.string(),
        std::string("curve.csv\0suffix", 16)
    };
#ifndef _WIN32
    fs::create_symlink(outside, work / "escape.csv");
    refused.push_back("escape.csv");
#endif
    for (const auto& source : refused) {
        CAPTURE(source);
        const auto result = chudware::piping::load_pump_curve(source);
        CHECK_FALSE(result.value("ok", true));
        CHECK(result.value("reason", "").find("cannot read pump curve: ") == 0);
    }
}

TEST_CASE("FEA system exports enforce root references before warm result-cache returns") {
    NativeFileFixture fixture;
    const fs::path work = fixture.root / "work";
    REQUIRE(fs::create_directory(work));
    NumericalRootPolicyGuard policy("");
    CacheSidecarGuard sidecars(fixture.root / "cache");
    const fs::path desktop_export = work / "desktop.json";
    json study = cube_static_study();
    study["export_system_path"] = desktop_export.string();
    const auto desktop = chudware::sim::fea_run_static(study);
    REQUIRE_MESSAGE(desktop.value("ok", false), desktop.dump());
    const std::string desktop_bytes = native_file_bytes(desktop_export);
    REQUIRE(chudware::sim::fea_run_static(study).value("cache_hit", false));

    chudware::doc::native_policy::configure(work.string(), "");
    const auto denied_cached = chudware::sim::fea_run_static(study);
    CHECK_FALSE(denied_cached.value("ok", true));
    CHECK(denied_cached.value("error", "").find("export_system_path refused: ") !=
          std::string::npos);
    CHECK(native_file_bytes(desktop_export) == desktop_bytes);

    const fs::path outside = fixture.root / "outside.json";
    const fs::path nul_target = work / "nul.json";
    for (const auto& path : {outside, nul_target}) {
        std::ofstream out(path);
        REQUIRE(out.good());
        out << "must remain untouched";
    }
    std::vector<std::string> refused{
        "../outside.json", "nested/../../outside.json", outside.string(),
        std::string("nul.json\0suffix", 15)
    };
#ifndef _WIN32
    fs::create_symlink(outside, work / "escape.json");
    refused.push_back("escape.json");
#endif
    for (const auto& path : refused) {
        CAPTURE(path);
        study["export_system_path"] = path;
        const auto result = chudware::sim::fea_run_static(study);
        CHECK_FALSE(result.value("ok", true));
        CHECK(result.value("error", "").find("export_system_path refused: ") !=
              std::string::npos);
        CHECK(native_file_bytes(outside) == "must remain untouched");
        CHECK(native_file_bytes(nul_target) == "must remain untouched");
    }
    study["export_system_path"] = "exports/system.json";
    const auto accepted = chudware::sim::fea_run_static(study);
    REQUIRE_MESSAGE(accepted.value("ok", false), accepted.dump());
    const json system = json::parse(native_file_bytes(work / "exports" / "system.json"));
    CHECK(system.at("points").size() == 8);
    CHECK(system.at("elems").size() == 6);
    CHECK(system.contains("fixed_nodes"));
}
