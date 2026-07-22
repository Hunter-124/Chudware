#include "firmware/hardware.h"

#include <chrono>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <stdexcept>

#include "firmware/subprocess.h"

namespace aicad::firmware {
namespace {

constexpr size_t kOutputCap = 32000;

std::string cap(const std::string& text) {
    if (text.size() <= kOutputCap) return text;
    return text.substr(0, kOutputCap) + "\n\xE2\x80\xA6[truncated]";
}

double elapsed_ms(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
}

FlashResult not_found(const std::string& tool, const std::vector<std::string>& cmd,
                      std::chrono::steady_clock::time_point t0) {
    FlashResult r;
    r.success = false;
    r.returncode = -1;
    r.cmd = cmd;
    r.output = "tool_not_found: '" + tool + "' is not installed / not on PATH";
    r.ms = elapsed_ms(t0);
    return r;
}

// hardware._run equivalent: returns (returncode, combined capped output).
FlashResult run_flash(const std::vector<std::string>& cmd, double timeout_s,
                      std::chrono::steady_clock::time_point t0) {
    FlashResult r;
    r.cmd = cmd;
    RunResult rr = run_capture(cmd, "", timeout_s);
    if (rr.not_found) {
        r.returncode = -1;
        r.output = "tool_not_found: executable '" + cmd[0] + "' not on PATH";
    } else if (rr.timed_out) {
        r.returncode = -1;
        r.output = "timeout: '" + cmd[0] + "' exceeded " +
                   std::to_string(static_cast<long long>(timeout_s)) + "s";
    } else {
        r.returncode = rr.returncode;
        r.output = cap(rr.stdout_text + rr.stderr_text);
    }
    r.success = (r.returncode == 0);
    r.ms = elapsed_ms(t0);
    return r;
}

}  // namespace

nlohmann::json FlashResult::to_dict() const {
    return {
        {"success", success},
        {"output", output},
        {"returncode", returncode},
        {"cmd", cmd},
        {"ms", std::round(ms * 100.0) / 100.0},
    };
}

FlashResult probe_rs_flash(const std::string& elf_path, const std::string& chip,
                           double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {"probe-rs", "download", "--chip", chip, elf_path};
    if (which("probe-rs").empty()) return not_found("probe-rs", cmd, t0);
    return run_flash(cmd, timeout_s, t0);
}

FlashResult probe_rs_reset(const std::string& chip, double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {"probe-rs", "reset", "--chip", chip};
    if (which("probe-rs").empty()) return not_found("probe-rs", cmd, t0);
    return run_flash(cmd, timeout_s, t0);
}

FlashResult probe_rs_halt(const std::string& chip, double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {"probe-rs", "reset", "--chip", chip, "--halt"};
    if (which("probe-rs").empty()) return not_found("probe-rs", cmd, t0);
    return run_flash(cmd, timeout_s, t0);
}

std::vector<uint8_t> probe_rs_read_memory(long long address, long long length,
                                          const std::string& chip, double timeout_s) {
    if (which("probe-rs").empty()) {
        throw std::runtime_error("tool_not_found: 'probe-rs' is not installed / not on PATH");
    }
    char addr_buf[32];
    std::snprintf(addr_buf, sizeof(addr_buf), "0x%llx", address);
    std::vector<std::string> cmd = {"probe-rs", "read", "--chip", chip, addr_buf,
                                    std::to_string(length)};
    RunResult rr = run_capture(cmd, "", timeout_s);
    if (rr.returncode != 0) {
        throw std::runtime_error("probe-rs read failed (rc=" + std::to_string(rr.returncode) +
                                 "): " + cap(rr.stdout_text + rr.stderr_text));
    }
    // probe-rs outputs hex bytes; parse them back to raw bytes. First try the
    // whitespace-stripped blob; on failure fall back to hex-chars-only.
    std::string output = rr.stdout_text + rr.stderr_text;
    auto from_hex = [](const std::string& h) -> std::vector<uint8_t> {
        std::vector<uint8_t> out;
        if (h.size() % 2) return {};
        out.reserve(h.size() / 2);
        for (size_t i = 0; i + 1 < h.size(); i += 2) {
            auto nib = [](char c) -> int {
                if (c >= '0' && c <= '9') return c - '0';
                if (c >= 'a' && c <= 'f') return c - 'a' + 10;
                if (c >= 'A' && c <= 'F') return c - 'A' + 10;
                return -1;
            };
            int hi = nib(h[i]), lo = nib(h[i + 1]);
            if (hi < 0 || lo < 0) return {};
            out.push_back(static_cast<uint8_t>((hi << 4) | lo));
        }
        return out;
    };
    std::string squeezed;
    for (char c : output)
        if (!std::isspace(static_cast<unsigned char>(c))) squeezed += c;
    std::vector<uint8_t> data = from_hex(squeezed);
    if (data.empty() && !squeezed.empty()) {
        std::string hex_only;
        for (char c : output)
            if (std::isxdigit(static_cast<unsigned char>(c))) hex_only += c;
        if (hex_only.size() % 2) hex_only.pop_back();
        data = from_hex(hex_only);
    }
    return data;
}

FlashResult esptool_flash(const std::string& bin_path, const std::string& port, int baud,
                          const std::string& flash_offset, double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {
        "esptool.py", "--port", port, "--baud", std::to_string(baud),
        "write_flash", flash_offset, bin_path,
    };
    if (which("esptool.py").empty()) return not_found("esptool.py", cmd, t0);
    return run_flash(cmd, timeout_s, t0);
}

}  // namespace aicad::firmware
