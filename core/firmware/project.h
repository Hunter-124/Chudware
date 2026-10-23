// Firmware project record — create/save/load (C++ port of
// engine_oracle/firmware/project.py). A project is a directory on disk
// (<firmware_root>/<project_id>/) seeded from the embedded cmake_arm template
// plus a project.json sidecar (schema aicad.firmware.project/1, atomic
// tmp+replace). See contracts/firmware_rpc.md §2/§6.
#pragma once

#include <filesystem>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

inline constexpr const char* kProjectSchema = "aicad.firmware.project/1";

struct Project {
    std::string project_id;
    std::string name;
    std::string mcu_family;
    std::string toolchain = "cmake";          // cmake | espidf | arduino
    std::optional<std::string> board_doc_ref;
    std::vector<std::string> src_paths;       // relative to root
    std::string build_dir = "build";          // relative to root
    double created_at = 0.0;
    std::filesystem::path root;               // absolute; not serialized

    std::filesystem::path root_dir() const;
    std::filesystem::path build_path() const { return root_dir() / build_dir; }
    std::filesystem::path sidecar() const { return root_dir() / "project.json"; }
    std::vector<std::filesystem::path> abs_sources() const;

    nlohmann::json to_dict() const;
    static Project from_dict(const nlohmann::json& d, const std::filesystem::path& root);
};

// Private per-user firmware directory: CHUDWARE_FIRMWARE_DIR override, otherwise
// APPDATA on Windows or XDG_DATA_HOME/HOME on POSIX. No shared temp fallback.
std::filesystem::path firmware_root();

// Atomically write project.json (tmp + rename). Returns the final path.
std::filesystem::path save(const Project& project);

// Load <firmware_root>/<project_id>/project.json. Throws std::runtime_error
// (FileNotFoundError equivalent) when absent/unreadable.
Project load(const std::string& project_id);

// Write the pin-map header to <build_dir>/include/pinmap.h; returns the path.
std::filesystem::path write_pinmap(const Project& project, const nlohmann::json& board_doc,
                                   const std::string& mcu_family = "");

// Create a new project: seed the embedded template, record src_paths, write
// the synthetic compile_commands.json (contract §6), persist project.json.
// When `mcu` is empty the family is auto-selected from the linked board's BOM;
// throws std::invalid_argument on an invalid toolchain or underivable family.
Project new_project(const std::optional<std::string>& board_doc_ref,
                    const std::string& mcu, const std::string& toolchain,
                    const std::string& name = "");

}  // namespace chudware::firmware
