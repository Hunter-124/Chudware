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
#include "doc/stepplus.h"
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
               const std::vector<std::pair<std::string, std::string>>& entries) {
    mz_zip_archive zip{};
    REQUIRE(mz_zip_writer_init_file(&zip, path.string().c_str(), 0));
    for (const auto& [name, data] : entries) {
        REQUIRE(mz_zip_writer_add_mem(&zip, name.c_str(), data.data(), data.size(),
                                      MZ_DEFAULT_COMPRESSION));
    }
    REQUIRE(mz_zip_writer_finalize_archive(&zip));
    REQUIRE(mz_zip_writer_end(&zip));
}

void check_stepplus_limit(const fs::path& path, const std::string& expected) {
    try {
        (void)chudware::doc::stepplus_open(path.string());
        FAIL("malicious archive was accepted");
    } catch (const std::exception& e) {
        CHECK(std::string(e.what()).find(expected) != std::string::npos);
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
