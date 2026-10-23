// Firmware toolchain wrappers — build a firmware::Project (C++ port of
// engine_oracle/firmware/toolchains.py). Every backend drives an unmodified
// external CLI as a subprocess (GPL-clean by aggregation,
// contracts/firmware_rpc.md §1). build() NEVER throws: a missing CLI, non-zero
// exit, or timeout comes back as BuildResult{success=false} (returncode -1
// marks tool-not-found/timeout).
#pragma once

#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "firmware/project.h"

namespace chudware::firmware {

// contracts/firmware_rpc.md §3.
struct BuildResult {
    bool success = false;
    std::optional<std::string> elf_path;
    std::optional<std::string> map_path;
    std::string stderr_text;
    std::vector<std::string> warnings;
    int returncode = 0;
    std::vector<std::string> cmd;
    double ms = 0.0;

    nlohmann::json to_dict() const;
};

// Build `project` with the backend named by project.toolchain
// (cmake | espidf | arduino).
BuildResult build(const Project& project, double timeout_s = 300.0);

}  // namespace chudware::firmware
