// Firmware ↔ board co-verification (C++ port of
// engine_oracle/firmware/coverify.py). Static pin checks + emulated-trace diff,
// findings in the ERC violation shape extended with a "fix" hint. Pure +
// advisory: deterministic ordering, never throws on a bad design. Rule catalog:
// contracts/firmware_rpc.md §10.
#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "firmware/project.h"

namespace chudware::firmware {

// {token: {"reads", "writes", "refs", "files"}} over {filename: text}.
// Deterministic key order (std::map) mirrors Python's sorted(sources.items()).
std::map<std::string, nlohmann::json> scan_firmware_pins(
    const std::map<std::string, std::string>& sources);

// Read a project's sources as {relpath: text}; unreadable files skipped.
std::map<std::string, std::string> project_sources(const Project& project);

// Load an aicad.firmware.trace/1 artifact; null json on any failure.
nlohmann::json load_trace(const std::string& trace_path);

// Full co-verification: static checks always; dynamic checks when `trace` is
// an object. Returns the merged, deterministically-ordered findings list.
nlohmann::json verify_board(const nlohmann::json& board,
                            const std::map<std::string, nlohmann::json>& fw_pins,
                            const nlohmann::json& sch,  // null when absent
                            const std::string& mcu_family,
                            const nlohmann::json& trace);  // null when absent

// {n, errors, warnings, rules} rollup.
nlohmann::json summarize(const nlohmann::json& findings);

}  // namespace chudware::firmware
