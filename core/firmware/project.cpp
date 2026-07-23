#include "firmware/project.h"

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>

#include "firmware/mcu_pinouts.h"
#include "firmware/pinmap.h"
#include "firmware/templates.h"

namespace chudware::firmware {
namespace {

namespace fs = std::filesystem;

// Source extensions the toolchains compile (mirrors project._SRC_EXTS).
const std::set<std::string> kSrcExts = {".c", ".cpp", ".cc", ".cxx", ".S", ".s"};

const char* kValidToolchains[] = {"cmake", "espidf", "arduino"};

std::string random_hex12() {
    std::random_device rd;
    std::mt19937_64 gen(rd());
    std::uniform_int_distribution<unsigned long long> dist;
    unsigned long long a = dist(gen), b = dist(gen);
    std::ostringstream oss;
    oss << std::hex;
    oss.width(16);
    oss.fill('0');
    oss << a;
    oss.width(16);
    oss.fill('0');
    oss << b;
    return oss.str().substr(0, 12);
}

void write_text(const fs::path& path, const std::string& text) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    out << text;
}

}  // namespace

fs::path Project::root_dir() const {
    return root.empty() ? (firmware_root() / project_id) : root;
}

std::vector<fs::path> Project::abs_sources() const {
    std::vector<fs::path> out;
    out.reserve(src_paths.size());
    for (const auto& p : src_paths) out.push_back(root_dir() / p);
    return out;
}

nlohmann::json Project::to_dict() const {
    nlohmann::json d;
    d["project_id"] = project_id;
    d["name"] = name;
    d["mcu_family"] = mcu_family;
    d["toolchain"] = toolchain;
    d["board_doc_ref"] = board_doc_ref ? nlohmann::json(*board_doc_ref) : nlohmann::json(nullptr);
    d["src_paths"] = src_paths;
    d["build_dir"] = build_dir;
    d["created_at"] = created_at;
    d["schema"] = kProjectSchema;
    return d;
}

Project Project::from_dict(const nlohmann::json& d, const fs::path& root_path) {
    Project p;
    p.project_id = d.value("project_id", std::string());
    p.name = d.value("name", p.project_id);
    p.mcu_family = d.value("mcu_family", std::string());
    p.toolchain = d.value("toolchain", std::string("cmake"));
    if (d.contains("board_doc_ref") && d["board_doc_ref"].is_string())
        p.board_doc_ref = d["board_doc_ref"].get<std::string>();
    if (d.contains("src_paths") && d["src_paths"].is_array())
        p.src_paths = d["src_paths"].get<std::vector<std::string>>();
    p.build_dir = d.value("build_dir", std::string("build"));
    p.created_at = d.value("created_at", 0.0);
    p.root = root_path;
    return p;
}

fs::path firmware_root() {
    fs::path d;
    if (const char* override_dir = std::getenv("CHUDWARE_FIRMWARE_DIR"); override_dir && *override_dir) {
        d = fs::path(override_dir);
    } else {
        const char* appdata = std::getenv("APPDATA");
        fs::path base = (appdata && *appdata) ? fs::path(appdata) : fs::temp_directory_path();
        d = base / "Chudware" / "firmware";
    }
    std::error_code ec;
    fs::create_directories(d, ec);
    return d;
}

fs::path save(const Project& project) {
    fs::path root = project.root_dir();
    std::error_code ec;
    fs::create_directories(root, ec);
    fs::path final_path = root / "project.json";
    fs::path tmp = root / ".project.json.tmp";
    // json.dump(indent=2, sort_keys=True) parity: nlohmann objects are ordered.
    write_text(tmp, project.to_dict().dump(2));
    fs::rename(tmp, final_path, ec);
    if (ec) {
        // rename across filesystems is impossible here (same dir); surface it.
        throw std::runtime_error("firmware save failed: " + ec.message());
    }
    return final_path;
}

Project load(const std::string& project_id) {
    fs::path base = firmware_root() / project_id;
    fs::path sidecar = base / "project.json";
    std::error_code ec;
    if (!fs::is_regular_file(sidecar, ec)) {
        throw std::runtime_error("no firmware project '" + project_id + "' at " +
                                 sidecar.string());
    }
    std::ifstream in(sidecar);
    std::stringstream ss;
    ss << in.rdbuf();
    nlohmann::json data = nlohmann::json::parse(ss.str(), nullptr, false);
    if (data.is_discarded() || !data.is_object())
        throw std::runtime_error("corrupt firmware project sidecar at " + sidecar.string());
    return Project::from_dict(data, base);
}

fs::path write_pinmap(const Project& project, const nlohmann::json& board_doc,
                      const std::string& mcu_family) {
    std::string fam = mcu_family.empty() ? project.mcu_family : mcu_family;
    std::string header = generate_pinmap(board_doc, fam);
    fs::path out = project.build_path() / "include" / "pinmap.h";
    write_text(out, header);
    return out;
}

Project new_project(const std::optional<std::string>& board_doc_ref,
                    const std::string& mcu_in, const std::string& toolchain,
                    const std::string& name) {
    bool valid = false;
    for (const char* t : kValidToolchains) valid = valid || toolchain == t;
    if (!valid) {
        throw std::invalid_argument(
            "toolchain must be one of ('cmake', 'espidf', 'arduino'), got '" + toolchain + "'");
    }

    std::string mcu = mcu_in;
    if (mcu.empty()) {
        nlohmann::json doc =
            board_doc_ref ? resolve_board_doc(*board_doc_ref) : nlohmann::json();
        if (doc.is_object()) {
            if (auto fam = select_mcu_family(doc)) mcu = *fam;
        }
        if (mcu.empty()) {
            throw std::invalid_argument(
                "new_project: no `mcu` given and none derivable from the ECAD BOM "
                "(need an STM32*/ESP32*/ATmega* MCU component in board_doc_ref, or pass mcu "
                "explicitly)");
        }
    }

    Project p;
    p.project_id = random_hex12();
    p.name = name.empty() ? ("firmware-" + p.project_id.substr(0, 6)) : name;
    p.mcu_family = mcu;
    p.toolchain = toolchain;
    p.board_doc_ref = board_doc_ref;
    p.build_dir = "build";
    p.created_at = std::chrono::duration<double>(
                       std::chrono::system_clock::now().time_since_epoch())
                       .count();
    p.root = firmware_root() / p.project_id;
    std::error_code ec;
    fs::create_directories(p.root, ec);

    // Install the embedded template (engine_oracle/firmware/templates/cmake_arm).
    for (const auto& tf : cmake_arm_template_files()) write_text(p.root / tf.relpath, tf.contents);
    for (const auto& tf : cmake_arm_template_files()) {
        fs::path rel(tf.relpath);
        std::string ext = rel.extension().string();
        if (kSrcExts.count(ext)) p.src_paths.push_back(tf.relpath);
    }
    std::sort(p.src_paths.begin(), p.src_paths.end());

    // Synthetic compile_commands.json (contract §6) — a real configure overwrites it.
    nlohmann::json entries = nlohmann::json::array();
    for (const auto& src : p.abs_sources()) {
        entries.push_back({
            {"directory", p.root_dir().string()},
            {"command", "arm-none-eabi-gcc -c " + src.generic_string()},
            {"file", src.string()},
        });
    }
    write_text(p.root / "compile_commands.json", entries.dump(2));

    save(p);
    return p;
}

}  // namespace chudware::firmware
