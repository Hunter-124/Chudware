#include "firmware/firmware_rpc.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>

#include "firmware/coverify.h"
#include "firmware/emulator.h"
#include "firmware/hardware.h"
#include "firmware/mcu_pinouts.h"
#include "firmware/pinmap.h"
#include "firmware/powerprofile.h"
#include "firmware/project.h"
#include "firmware/provision.h"
#include "firmware/scenario.h"
#include "firmware/sim_sensor_bridge.h"
#include "firmware/toolchains.h"
#include "firmware/private_storage.h"

namespace chudware::firmware {
namespace {

namespace fs = std::filesystem;

nlohmann::json err_env(const std::string& code, const std::string& message,
                       const std::string& detail = "") {
    return {{"code", code}, {"message", message}, {"detail", detail}};
}

std::string jstr(const nlohmann::json& j, const char* key) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return "";
    return it->get<std::string>();
}

bool has(const nlohmann::json& j, const char* key) {
    auto it = j.find(key);
    return it != j.end() && !it->is_null();
}

// --- fw_new_project ---------------------------------------------------------

nlohmann::json rpc_new_project(const nlohmann::json& params) {
    std::string name = jstr(params, "name");
    std::string mcu = jstr(params, "mcu_family");
    if (mcu.empty()) mcu = jstr(params, "mcu");
    std::string toolchain = jstr(params, "toolchain");
    if (toolchain.empty()) toolchain = "cmake";
    std::string board_ref = jstr(params, "board_doc_ref");

    Project project;
    try {
        project = new_project(board_ref.empty() ? std::optional<std::string>()
                                                : std::optional<std::string>(board_ref),
                              mcu, toolchain, name);
    } catch (const std::invalid_argument& e) {
        return err_env("invalid_request", e.what());
    }

    nlohmann::json result = {{"project_id", project.project_id}, {"project", project.to_dict()}};
    // Seed the pin-map header from the linked board's netlist (never fatal).
    if (!board_ref.empty()) {
        try {
            nlohmann::json doc = resolve_board_doc(board_ref);
            if (doc.is_object()) {
                fs::path path = write_pinmap(project, doc, project.mcu_family);
                result["pinmap_path"] = path.string();
            }
        } catch (...) {
        }
    }
    return result;
}

// --- fw_build ---------------------------------------------------------------

nlohmann::json rpc_build(const nlohmann::json& params) {
    std::string project_id = jstr(params, "project_id");
    if (project_id.empty()) return err_env("invalid_request", "fw_build requires 'project_id'");
    Project project;
    try {
        project = load(project_id);
    } catch (const std::exception& e) {
        return err_env("unknown_project", e.what());
    }
    return build(project).to_dict();
}

// --- fw_sources -------------------------------------------------------------

nlohmann::json rpc_sources(const nlohmann::json& params) {
    std::string project_id = jstr(params, "project_id");
    if (project_id.empty()) return err_env("invalid_request", "fw_sources requires 'project_id'");
    Project project;
    try {
        project = load(project_id);
    } catch (const std::exception& e) {
        return err_env("unknown_project", e.what());
    }

    std::vector<std::string> add, remove;
    if (params.contains("add") && params["add"].is_array())
        for (const auto& p : params["add"])
            if (p.is_string()) add.push_back(p.get<std::string>());
    if (params.contains("remove") && params["remove"].is_array())
        for (const auto& p : params["remove"])
            if (p.is_string()) remove.push_back(p.get<std::string>());
    bool changed = false;
    for (const auto& rel : add) {
        const fs::path path(rel);
        if (path.empty() || path.is_absolute() || path.has_root_path() ||
            rel.find('\\') != std::string::npos || rel.find(':') != std::string::npos ||
            rel.find('\0') != std::string::npos ||
            std::any_of(path.begin(), path.end(), [](const auto& component) {
                return component == ".." || component == ".";
            }))
            return err_env("invalid_request", "firmware source must be a contained relative path");
    }

    for (const auto& rel : remove) {
        auto it = std::find(project.src_paths.begin(), project.src_paths.end(), rel);
        if (it != project.src_paths.end()) {
            project.src_paths.erase(it);
            changed = true;
        }
    }
    for (const auto& rel : add) {
        if (std::find(project.src_paths.begin(), project.src_paths.end(), rel) ==
            project.src_paths.end()) {
            project.src_paths.push_back(rel);
            changed = true;
        }
        // New sources are intentionally empty until supplied by the editor.
        const fs::path abs_path = project.root_dir() / rel;
        std::string existing;
        if (!private_storage_detail::read_file(abs_path, existing))
            private_storage_detail::atomic_write(abs_path, "");
    }
    if (changed) {
        std::sort(project.src_paths.begin(), project.src_paths.end());
        save(project);
    }
    return {{"src_paths", project.src_paths}};
}

// --- fw_sim_run -------------------------------------------------------------

// First file under `dir` matching any of the suffixes, sorted rglob order
// (mirrors mcp_tools fw_sim_run / fw_verify_board ELF discovery).
std::string first_artifact(const fs::path& dir) {
    std::error_code ec;
    for (const char* ext : {".elf", ".bin", ".hex"}) {
        std::vector<fs::path> hits;
        if (fs::is_directory(dir, ec)) {
            for (auto it = fs::recursive_directory_iterator(dir, ec);
                 !ec && it != fs::recursive_directory_iterator(); it.increment(ec)) {
                if (it->is_regular_file(ec) && it->path().extension() == ext)
                    hits.push_back(it->path());
            }
        }
        std::sort(hits.begin(), hits.end());
        if (!hits.empty()) return hits.front().string();
    }
    return "";
}

nlohmann::json rpc_sim_run(const nlohmann::json& params) {
    std::string project_id = jstr(params, "project_id");
    if (project_id.empty()) return err_env("invalid_request", "fw_sim_run requires 'project_id'");
    Project project;
    try {
        project = load(project_id);
    } catch (const std::exception& e) {
        return err_env("unknown_project", e.what());
    }

    // Resolve ELF: explicit param > glob the build dir.
    std::string elf_path = jstr(params, "elf_path");
    if (elf_path.empty()) elf_path = first_artifact(project.build_path());
    if (elf_path.empty()) {
        SimResult r;
        r.passed = false;
        r.uart_log = "no_elf: no .elf found in build dir — run fw_build first";
        return r.to_dict();
    }

    std::vector<std::string> assertions;
    if (params.contains("assertions") && params["assertions"].is_array())
        for (const auto& a : params["assertions"])
            if (a.is_string()) assertions.push_back(a.get<std::string>());

    // Scenario: dict, JSON string, or fixture reference → validated dict.
    nlohmann::json scenario_input=params.contains("scenario")?params["scenario"]:nlohmann::json();
    const std::string scenario_path=jstr(params,"scenario_path");
    if(!scenario_path.empty()) {
        if(!scenario_input.is_null() && scenario_input!=nlohmann::json(""))
            return err_env("invalid_scenario","provide scenario or scenario_path, not both");
        std::ifstream input(scenario_path,std::ios::binary);
        if(!input) return err_env("invalid_scenario","cannot read scenario_path");
        scenario_input=std::string(std::istreambuf_iterator<char>(input),std::istreambuf_iterator<char>());
    }
    auto [scenario, scenario_errors] = load_scenario(scenario_input);
    if (!scenario_errors.empty()) {
        std::string joined;
        for (const auto& e : scenario_errors) {
            if (!joined.empty()) joined += "; ";
            joined += e;
        }
        return err_env("invalid_scenario", "scenario failed aicad.fwscenario/1 validation",
                       joined);
    }

    SimResult result = run_simulation(elf_path,
                                      project.mcu_family.empty() ? "cortex-m4"
                                                                 : project.mcu_family,
                                      assertions, params.value("timeout_s", 30.0), scenario,
                                      params.value("run_s",0.0),params.value("backend",std::string("auto")));
    nlohmann::json out = result.to_dict();

    // Emit the aicad.powerprofile/1 artifact when the project is linked
    // to a board and a trace was written. Best-effort — never fails the sim.
    if (result.trace_path && project.board_doc_ref) {
        try {
            nlohmann::json board = resolve_board_doc(*project.board_doc_ref);
            if (board.is_object()) {
                nlohmann::json trace = load_trace(*result.trace_path);
                if (trace.is_object()) {
                    nlohmann::json profile =
                        build_power_profile(trace, board, project.mcu_family,
                                            nlohmann::json(*result.trace_path));
                    auto pp = write_profile_artifact(profile, elf_path);
                    out["power_profile_path"] =
                        pp ? nlohmann::json(*pp) : nlohmann::json(nullptr);
                }
            }
        } catch (...) {
        }
    }
    return out;
}

// --- fw_power_profile ---------------------------------------------------------

nlohmann::json rpc_power_profile(const nlohmann::json& params) {
    std::string trace_path = jstr(params, "trace_path");
    if (trace_path.empty())
        return err_env("invalid_request", "fw_power_profile requires 'trace_path'");
    nlohmann::json trace = load_trace(trace_path);
    if (!trace.is_object()) {
        return err_env("invalid_trace", "trace_path '" + trace_path +
                                        "' is not a readable JSON trace artifact");
    }

    std::optional<Project> project;
    if (has(params, "project_id")) {
        try {
            project = load(jstr(params, "project_id"));
        } catch (const std::exception& e) {
            return err_env("unknown_project", e.what());
        }
    }

    std::string board_ref = jstr(params, "board_doc_ref");
    if (board_ref.empty() && project && project->board_doc_ref)
        board_ref = *project->board_doc_ref;
    nlohmann::json board = board_ref.empty() ? nlohmann::json() : resolve_board_doc(board_ref);
    std::string mcu_family = jstr(params, "mcu_family");
    if (mcu_family.empty() && project) mcu_family = project->mcu_family;
    if (mcu_family.empty() && board.is_object()) {
        if (auto fam = select_mcu_family(board)) mcu_family = *fam;
    }

    double window = params.value("window_ms", 0.0);
    return build_power_profile(trace, board, mcu_family, nlohmann::json(trace_path), window);
}

// --- fw_sensor_scenario -------------------------------------------------------

nlohmann::json rpc_sensor_scenario(const nlohmann::json& params) {
    nlohmann::json profile;
    if (params.contains("profile") && !params["profile"].is_null())
        profile = params["profile"];
    else if (params.contains("sim_result") && !params["sim_result"].is_null())
        profile = params["sim_result"];
    else {
        // allow the whole params body (minus control keys) as the profile
        nlohmann::json maybe = nlohmann::json::object();
        for (auto& [k, v] : params.items()) {
            if (k != "sensor_map" && k != "duration_s" && k != "dt_s" && k != "name" &&
                k != "profile" && k != "sim_result")
                maybe[k] = v;
        }
        if (maybe.contains("t_s") || maybe.contains("samples") ||
            maybe.contains("altitude_m") || maybe.contains("airspeed_m_s"))
            profile = maybe;
    }
    if (profile.is_null()) {
        return err_env("invalid_request",
                       "fw_sensor_scenario requires 'profile' (or 'sim_result') and 'sensor_map'");
    }
    if (!params.contains("sensor_map") || params["sensor_map"].is_null() ||
        (params["sensor_map"].is_array() && params["sensor_map"].empty())) {
        return err_env("invalid_request", "fw_sensor_scenario requires non-empty 'sensor_map'");
    }
    double dt = params.value("dt_s", 0.01);
    std::string name = jstr(params, "name");
    nlohmann::json duration = params.contains("duration_s") ? params["duration_s"]
                                                            : nlohmann::json();

    auto t0 = std::chrono::steady_clock::now();
    nlohmann::json scenario;
    try {
        scenario = build_sensor_scenario(profile, params["sensor_map"], duration, dt, name);
    } catch (const std::exception& e) {
        return err_env("invalid_request", e.what());
    }
    double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
                    .count();
    nlohmann::json meta = scenario.value("meta", nlohmann::json::object());
    return {
        {"scenario", scenario},
        {"n_tracks", scenario["tracks"].size()},
        {"devices", meta.value("devices", nlohmann::json::array())},
        {"dt_s", meta.value("dt_s", dt)},
        {"duration_s", meta.value("t1_s", 0.0) - meta.value("t0_s", 0.0)},
        {"ms", std::round(ms * 100.0) / 100.0},
    };
}

// --- fw_verify_board --------------------------------------------------------

nlohmann::json rpc_verify_board(const nlohmann::json& params) {
    std::string board_ref = jstr(params, "board_doc_ref");
    nlohmann::json board = resolve_board_doc(board_ref);
    if (!board.is_object()) {
        return err_env("unknown_board", "board_doc_ref '" + board_ref +
                                        "' did not resolve to an open ECAD doc or a readable file");
    }
    nlohmann::json sch;
    if (has(params, "sch_doc_ref")) sch = resolve_board_doc(jstr(params, "sch_doc_ref"));

    std::optional<Project> project;
    if (has(params, "project_id")) {
        try {
            project = load(jstr(params, "project_id"));
        } catch (const std::exception& e) {
            return err_env("unknown_project", e.what());
        }
    }

    std::map<std::string, std::string> sources;
    if (params.contains("sources") && params["sources"].is_object()) {
        for (auto it = params["sources"].begin(); it != params["sources"].end(); ++it) {
            if (it.value().is_string()) sources[it.key()] = it.value().get<std::string>();
        }
    } else if (project) {
        sources = project_sources(*project);
    }
    for (auto it = sources.begin(); it != sources.end();) {
        const std::string& k = it->first;
        if (k.size() >= 8 && k.compare(k.size() - 8, 8, "pinmap.h") == 0)
            it = sources.erase(it);
        else
            ++it;
    }
    if (sources.empty()) {
        return err_env("invalid_request",
                       "fw_verify_board needs firmware sources: pass 'sources' or a 'project_id' "
                       "whose project has source files");
    }

    std::string mcu_family = jstr(params, "mcu_family");
    if (mcu_family.empty() && project) mcu_family = project->mcu_family;
    if (mcu_family.empty()) {
        if (auto fam = select_mcu_family(board)) mcu_family = *fam;
    }
    auto fw_pins = scan_firmware_pins(sources);

    // Dynamic pass: existing trace artifact, or a real emulation when asked.
    nlohmann::json trace;
    nlohmann::json dynamic = {{"ran", false}, {"source", nullptr}};
    if (has(params, "trace_path")) {
        std::string tp = jstr(params, "trace_path");
        trace = load_trace(tp);
        if (!trace.is_object())
            dynamic["reason"] = "trace_path '" + tp + "' unreadable";
        else
            dynamic = {{"ran", true}, {"source", "trace"}};
    } else if (params.value("emulate", false) && project) {
        std::string elf = first_artifact(project->build_path());
        if (elf.empty()) {
            dynamic["reason"] = "no_elf: run fw_build first";
        } else {
            // Synthetic gpio assertions install the Renode pin hooks for every pin the
            // firmware writes; we only consume the observed events, not passed/failed.
            std::vector<std::string> hooks;
            for (const auto& [tok, use] : fw_pins) {
                if (use.value("writes", 0L) > 0 && resolve_pin(tok, mcu_family))
                    hooks.push_back("gpio:" + tok + "=1");
            }
            std::sort(hooks.begin(), hooks.end());
            SimResult sim = run_simulation(elf, mcu_family.empty() ? "cortex-m4" : mcu_family,
                                           hooks, params.value("timeout_s", 30.0));
            if ((sim.gpio_events.is_array() && !sim.gpio_events.empty()) ||
                (sim.gpio_states.is_object() && !sim.gpio_states.empty())) {
                trace = {{"events", sim.gpio_events}, {"gpio_final", sim.gpio_states}};
                dynamic = {{"ran", true}, {"source", "emulation"}};
            } else {
                std::string reason = sim.uart_log.substr(0, 200);
                dynamic["reason"] =
                    reason.empty() ? "emulation produced no pin events" : reason;
            }
        }
    }

    nlohmann::json fw_pins_json = nlohmann::json::object();
    for (const auto& [tok, rec] : fw_pins) fw_pins_json[tok] = rec;
    nlohmann::json findings = verify_board(board, fw_pins, sch, mcu_family, trace);
    return {
        {"ok", true},
        {"mcu_family", mcu_family.empty() ? nlohmann::json(nullptr) : nlohmann::json(mcu_family)},
        {"findings", findings},
        {"summary", summarize(findings)},
        {"fw_pins", fw_pins_json},
        {"dynamic", dynamic},
    };
}

// --- hardware path ----------------------------------------------------------

nlohmann::json rpc_flash(const nlohmann::json& params) {
    std::string elf_path = jstr(params, "elf_path");
    if (elf_path.empty()) elf_path = jstr(params, "bin_path");
    if (elf_path.empty()) return err_env("invalid_request", "fw_flash requires 'elf_path'");
    std::string chip = jstr(params, "chip");
    std::string port = jstr(params, "port");
    double timeout_s = params.value("timeout_s", 60.0);

    // Route: esptool for explicit serial port + .bin; probe-rs otherwise.
    bool is_bin = elf_path.size() >= 4 && elf_path.compare(elf_path.size() - 4, 4, ".bin") == 0;
    if (!port.empty() && is_bin) {
        int baud = params.value("baud", 460800);
        std::string flash_offset = jstr(params, "flash_offset");
        if (flash_offset.empty()) flash_offset = "0x0";
        return esptool_flash(elf_path, port, baud, flash_offset, timeout_s).to_dict();
    }
    if (chip.empty())
        return err_env("invalid_request", "fw_flash requires 'chip' for probe-rs flashing");
    return probe_rs_flash(elf_path, chip, timeout_s).to_dict();
}

nlohmann::json rpc_debug_start(const nlohmann::json& params) {
    std::string chip = jstr(params, "chip");
    if (chip.empty()) return err_env("invalid_request", "fw_debug_start requires 'chip'");
    return probe_rs_reset(chip, params.value("timeout_s", 30.0)).to_dict();
}

nlohmann::json rpc_debug_halt(const nlohmann::json& params) {
    std::string chip = jstr(params, "chip");
    if (chip.empty()) return err_env("invalid_request", "fw_debug_halt requires 'chip'");
    return probe_rs_halt(chip, params.value("timeout_s", 30.0)).to_dict();
}

long long parse_int_base0(const std::string& s) {
    // int(str, 0): accept "0x20000000" or decimal (and 0o/0b for completeness).
    size_t idx = 0;
    long long v = std::stoll(s, &idx, 0);
    while (idx < s.size() && std::isspace(static_cast<unsigned char>(s[idx]))) ++idx;
    if (idx != s.size()) throw std::invalid_argument("trailing characters");
    return v;
}

std::string hex_ll(long long v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "0x%llx", v);
    return buf;
}

nlohmann::json rpc_debug_read_mem(const nlohmann::json& params) {
    std::string chip = jstr(params, "chip");
    if (chip.empty()) return err_env("invalid_request", "fw_debug_read_mem requires 'chip'");
    if (!params.contains("address") || !params.contains("length") ||
        params["address"].is_null() || params["length"].is_null()) {
        return err_env("invalid_request", "fw_debug_read_mem requires 'address' and 'length'");
    }
    long long address, length;
    try {
        address = params["address"].is_number()
                      ? params["address"].get<long long>()
                      : parse_int_base0(params["address"].get<std::string>());
        length = params["length"].is_number()
                     ? params["length"].get<long long>()
                     : parse_int_base0(params["length"].get<std::string>());
    } catch (const std::exception& e) {
        return err_env("invalid_request", std::string("invalid address or length: ") + e.what());
    }

    auto t0 = std::chrono::steady_clock::now();
    auto ms = [&t0]() {
        return std::round(
                   std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
                       .count() *
                   100.0) /
               100.0;
    };
    try {
        std::vector<uint8_t> data =
            probe_rs_read_memory(address, length, chip, params.value("timeout_s", 30.0));
        static const char* hexd = "0123456789abcdef";
        std::string hex;
        hex.reserve(data.size() * 2);
        for (uint8_t b : data) {
            hex += hexd[b >> 4];
            hex += hexd[b & 0xF];
        }
        return {
            {"success", true},
            {"data_hex", hex},
            {"address", hex_ll(address)},
            {"length", static_cast<long long>(data.size())},
            {"ms", ms()},
        };
    } catch (const std::runtime_error& e) {
        return {
            {"success", false},
            {"reason", e.what()},
            {"address", hex_ll(address)},
            {"length", length},
            {"ms", ms()},
        };
    }
}

// --- avrdude flash (S-FW-CORE) ------------------------------------------------

nlohmann::json rpc_avr_flash(const nlohmann::json& params) {
    std::string hex_path = jstr(params, "hex_path");
    if (hex_path.empty()) hex_path = jstr(params, "elf_path");
    if (hex_path.empty()) return err_env("invalid_request", "fw_avr_flash requires 'hex_path'");
    std::string mcu = jstr(params, "mcu");
    if (mcu.empty()) return err_env("invalid_request", "fw_avr_flash requires 'mcu'");
    std::string programmer = jstr(params, "programmer");
    std::string port = jstr(params, "port");
    int baud = params.value("baud", 115200);
    double timeout_s = params.value("timeout_s", 60.0);
    return avrdude_flash(hex_path, mcu, programmer, port, baud, timeout_s).to_dict();
}

// --- openocd + gdb/MI debug (S-FW-CORE) ---------------------------------------

std::vector<std::string> jstrvec(const nlohmann::json& params, const char* key) {
    std::vector<std::string> out;
    auto it = params.find(key);
    if (it == params.end() || !it->is_array()) return out;
    for (const auto& v : *it)
        if (v.is_string()) out.push_back(v.get<std::string>());
    return out;
}

nlohmann::json rpc_debug_gdb_reset(const nlohmann::json& params) {
    std::string cfg = jstr(params, "openocd_cfg");
    if (cfg.empty()) return err_env("invalid_request", "fw_debug_gdb_reset requires 'openocd_cfg'");
    bool halt = params.value("halt", true);
    return openocd_gdb_reset(cfg, halt, params.value("timeout_s", 30.0)).to_dict();
}

nlohmann::json rpc_debug_gdb_halt(const nlohmann::json& params) {
    std::string cfg = jstr(params, "openocd_cfg");
    if (cfg.empty()) return err_env("invalid_request", "fw_debug_gdb_halt requires 'openocd_cfg'");
    return openocd_gdb_halt(cfg, params.value("timeout_s", 30.0)).to_dict();
}

nlohmann::json rpc_debug_gdb_step(const nlohmann::json& params) {
    std::string cfg = jstr(params, "openocd_cfg");
    if (cfg.empty()) return err_env("invalid_request", "fw_debug_gdb_step requires 'openocd_cfg'");
    bool step_over = params.value("step_over", false);
    return openocd_gdb_step(cfg, step_over, params.value("timeout_s", 30.0)).to_dict();
}

nlohmann::json rpc_debug_gdb_set_breakpoint(const nlohmann::json& params) {
    std::string cfg = jstr(params, "openocd_cfg");
    if (cfg.empty())
        return err_env("invalid_request", "fw_debug_gdb_set_breakpoint requires 'openocd_cfg'");
    std::string address = jstr(params, "address");
    if (address.empty())
        return err_env("invalid_request", "fw_debug_gdb_set_breakpoint requires 'address'");
    return openocd_gdb_set_breakpoint(cfg, address, params.value("timeout_s", 30.0)).to_dict();
}

nlohmann::json rpc_debug_gdb_read_mem(const nlohmann::json& params) {
    std::string cfg = jstr(params, "openocd_cfg");
    if (cfg.empty())
        return err_env("invalid_request", "fw_debug_gdb_read_mem requires 'openocd_cfg'");
    std::string address = jstr(params, "address");
    if (address.empty())
        return err_env("invalid_request", "fw_debug_gdb_read_mem requires 'address'");
    if (!params.contains("length") || params["length"].is_null())
        return err_env("invalid_request", "fw_debug_gdb_read_mem requires 'length'");
    long long length = params["length"].is_number()
                            ? params["length"].get<long long>()
                            : std::stoll(params["length"].get<std::string>());
    return openocd_gdb_read_mem(cfg, address, length, params.value("timeout_s", 30.0)).to_dict();
}

// --- simavr / renode emulation (S-FW-CORE) -------------------------------------

nlohmann::json rpc_emulate_simavr(const nlohmann::json& params) {
    std::string elf_path = jstr(params, "elf_path");
    if (elf_path.empty()) return err_env("invalid_request", "fw_emulate_simavr requires 'elf_path'");
    std::string mcu = jstr(params, "mcu");
    if (mcu.empty()) return err_env("invalid_request", "fw_emulate_simavr requires 'mcu'");
    double run_s = params.value("run_s", 2.0);
    int gdb_port = params.value("gdb_port", 1234);
    double timeout_s = params.value("timeout_s", run_s + 10.0);
    std::vector<std::string> mi_commands =
        params.value("gdb", false) ? jstrvec(params, "mi_commands") : std::vector<std::string>{};
    std::string gdb_binary = jstr(params, "gdb_binary");
    if (gdb_binary.empty()) gdb_binary = "avr-gdb";
    return simavr_run(elf_path, mcu, run_s, gdb_port, mi_commands, timeout_s, gdb_binary).to_dict();
}

nlohmann::json rpc_emulate_renode(const nlohmann::json& params) {
    std::string resc_path = jstr(params, "resc_path");
    if (resc_path.empty())
        return err_env("invalid_request", "fw_emulate_renode requires 'resc_path'");
    double run_s = params.value("run_s", 2.0);
    int gdb_port = params.value("gdb_port", 3333);
    double timeout_s = params.value("timeout_s", run_s + 10.0);
    std::vector<std::string> mi_commands =
        params.value("gdb", false) ? jstrvec(params, "mi_commands") : std::vector<std::string>{};
    std::string gdb_binary = jstr(params, "gdb_binary");
    if (gdb_binary.empty()) gdb_binary = "arm-none-eabi-gdb";
    return renode_run(resc_path, run_s, gdb_port, mi_commands, timeout_s, gdb_binary).to_dict();
}

// --- live serial monitor (S-FW-CORE) --------------------------------------------

std::string bytes_to_hex(const std::string& data) {
    static const char* hexd = "0123456789abcdef";
    std::string hex;
    hex.reserve(data.size() * 2);
    for (unsigned char b : data) {
        hex += hexd[b >> 4];
        hex += hexd[b & 0xF];
    }
    return hex;
}

bool hex_to_bytes(const std::string& hex, std::string& out) {
    if (hex.size() % 2) return false;
    out.clear();
    out.reserve(hex.size() / 2);
    auto nib = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    for (size_t i = 0; i + 1 < hex.size(); i += 2) {
        int hi = nib(hex[i]), lo = nib(hex[i + 1]);
        if (hi < 0 || lo < 0) return false;
        out.push_back(static_cast<char>((hi << 4) | lo));
    }
    return true;
}

nlohmann::json rpc_serial_open(const nlohmann::json& params) {
    std::string port = jstr(params, "port");
    if (port.empty()) return err_env("invalid_request", "fw_serial_open requires 'port'");
    int baud = params.value("baud", 115200);
    std::string open_err;
    int handle = serial_open(port, baud, open_err);
    if (handle < 0) return {{"success", false}, {"reason", open_err}};
    return {{"success", true}, {"handle", handle}, {"port", port}, {"baud", baud}};
}

nlohmann::json rpc_serial_read(const nlohmann::json& params) {
    if (!params.contains("handle") || !params["handle"].is_number())
        return err_env("invalid_request", "fw_serial_read requires 'handle'");
    int handle = params["handle"].get<int>();
    size_t max_bytes = static_cast<size_t>(params.value("max_bytes", 4096));
    try {
        std::string data = serial_read(handle, max_bytes);
        return {{"success", true}, {"data_hex", bytes_to_hex(data)}, {"bytes", data.size()}};
    } catch (const std::exception& e) {
        return {{"success", false}, {"reason", e.what()}};
    }
}

nlohmann::json rpc_serial_write(const nlohmann::json& params) {
    if (!params.contains("handle") || !params["handle"].is_number())
        return err_env("invalid_request", "fw_serial_write requires 'handle'");
    int handle = params["handle"].get<int>();
    if (!has(params, "data_hex") && !has(params, "text"))
        return err_env("invalid_request", "fw_serial_write requires 'data_hex' or 'text'");
    std::string payload;
    if (has(params, "data_hex")) {
        if (!hex_to_bytes(jstr(params, "data_hex"), payload))
            return err_env("invalid_request", "fw_serial_write: 'data_hex' is not valid hex");
    } else {
        payload = jstr(params, "text");
    }
    try {
        size_t n = serial_write(handle, payload);
        return {{"success", true}, {"bytes_written", n}};
    } catch (const std::exception& e) {
        return {{"success", false}, {"reason", e.what()}};
    }
}

nlohmann::json rpc_serial_close(const nlohmann::json& params) {
    if (!params.contains("handle") || !params["handle"].is_number())
        return err_env("invalid_request", "fw_serial_close requires 'handle'");
    int handle = params["handle"].get<int>();
    return {{"success", serial_close(handle)}};
}

// --- fw_projects (worker/GUI convenience) ------------------------------------

nlohmann::json rpc_projects() {
    fs::path root = firmware_root();
    nlohmann::json projects = nlohmann::json::array();
    std::error_code ec;
    if (fs::is_directory(root, ec)) {
        std::vector<fs::path> children;
        for (auto it = fs::directory_iterator(root, ec); !ec && it != fs::directory_iterator();
             it.increment(ec)) {
            children.push_back(it->path());
        }
        std::sort(children.begin(), children.end());
        for (const auto& child : children) {
            if (!fs::is_regular_file(child / "project.json", ec)) continue;
            try {
                Project p = load(child.filename().string());
                nlohmann::json d = p.to_dict();
                d["root_dir"] = p.root_dir().string();
                projects.push_back(d);
            } catch (...) {
                continue;  // skip corrupt sidecars, keep listing
            }
        }
    }
    std::sort(projects.begin(), projects.end(), [](const nlohmann::json& a, const nlohmann::json& b) {
        return a.value("created_at", 0.0) > b.value("created_at", 0.0);
    });
    return {{"projects", projects}};
}

}  // namespace

bool call(const std::string& method, const nlohmann::json& params, nlohmann::json& result,
          std::string& err) {
    (void)err;
    try {
        if (method == "fw_new_project") {
            result = rpc_new_project(params);
        } else if (method == "fw_build") {
            result = rpc_build(params);
        } else if (method == "fw_sources") {
            result = rpc_sources(params);
        } else if (method == "fw_verify_board") {
            result = rpc_verify_board(params);
        } else if (method == "fw_sim_run") {
            result = rpc_sim_run(params);
        } else if (method == "fw_power_profile") {
            result = rpc_power_profile(params);
        } else if (method == "fw_sensor_scenario") {
            result = rpc_sensor_scenario(params);
        } else if (method == "fw_flash") {
            result = rpc_flash(params);
        } else if (method == "fw_debug_start") {
            result = rpc_debug_start(params);
        } else if (method == "fw_debug_halt") {
            result = rpc_debug_halt(params);
        } else if (method == "fw_debug_read_mem") {
            result = rpc_debug_read_mem(params);
        } else if (method == "fw_avr_flash") {
            result = rpc_avr_flash(params);
        } else if (method == "fw_debug_gdb_reset") {
            result = rpc_debug_gdb_reset(params);
        } else if (method == "fw_debug_gdb_halt") {
            result = rpc_debug_gdb_halt(params);
        } else if (method == "fw_debug_gdb_step") {
            result = rpc_debug_gdb_step(params);
        } else if (method == "fw_debug_gdb_set_breakpoint") {
            result = rpc_debug_gdb_set_breakpoint(params);
        } else if (method == "fw_debug_gdb_read_mem") {
            result = rpc_debug_gdb_read_mem(params);
        } else if (method == "fw_emulate_simavr") {
            result = rpc_emulate_simavr(params);
        } else if (method == "fw_emulate_renode") {
            result = rpc_emulate_renode(params);
        } else if (method == "fw_serial_open") {
            result = rpc_serial_open(params);
        } else if (method == "fw_serial_read") {
            result = rpc_serial_read(params);
        } else if (method == "fw_serial_write") {
            result = rpc_serial_write(params);
        } else if (method == "fw_serial_close") {
            result = rpc_serial_close(params);
        } else if (method == "fw_status") {
            result = fw_status();
        } else if (method == "fw_projects") {
            result = rpc_projects();
        } else {
            return false;  // not a ported firmware method — fall through
        }
        return true;
    } catch (const std::exception& e) {
        // Defensive: the firmware contract wants expected failures inside the
        // result envelope, never a raised dispatch error.
        result = err_env("internal_error", e.what());
        return true;
    }
}

}  // namespace chudware::firmware
