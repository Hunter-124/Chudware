// Hardware flashing and debug wrappers (T-RF15 port of
// engine_oracle/firmware/hardware.py). probe-rs (MIT/Apache-2.0) and esptool
// (GPL-2.0+) are SUBPROCESS tools — never linked. A missing binary is
// graceful: FlashResult{success=false}, never an error envelope or a throw.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad::firmware {

// Outcome of a flash/debug operation (mirrors hardware.FlashResult).
struct FlashResult {
    bool success = false;
    std::string output;      // combined stdout+stderr, capped at 32k
    int returncode = 0;      // -1 = binary absent or timed out
    std::vector<std::string> cmd;
    double ms = 0.0;

    nlohmann::json to_dict() const;
};

// probe-rs download --chip <chip> <elf>
FlashResult probe_rs_flash(const std::string& elf_path, const std::string& chip,
                           double timeout_s = 60.0);
// probe-rs reset --chip <chip>
FlashResult probe_rs_reset(const std::string& chip, double timeout_s = 60.0);
// probe-rs reset --chip <chip> --halt
FlashResult probe_rs_halt(const std::string& chip, double timeout_s = 60.0);

// probe-rs read --chip <chip> <addr> <len>. Returns raw bytes on success;
// throws std::runtime_error on tool-absent/failure (the fw_debug_read_mem RPC
// wrapper catches it into the graceful {success:false, reason} shape, exactly
// like the Python handler).
std::vector<uint8_t> probe_rs_read_memory(long long address, long long length,
                                          const std::string& chip, double timeout_s = 60.0);

// esptool.py --port <port> --baud <baud> write_flash <offset> <bin>
FlashResult esptool_flash(const std::string& bin_path, const std::string& port, int baud = 460800,
                          const std::string& flash_offset = "0x0", double timeout_s = 60.0);

}  // namespace aicad::firmware
