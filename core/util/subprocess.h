#pragma once

#include <chrono>
#include <cstddef>
#include <map>
#include <string>
#include <vector>

namespace chudware::util {

// Runs argv directly, without a shell. Environment entries override the inherited environment.
struct ProcessOptions {
    std::chrono::milliseconds timeout{30000};
    std::size_t max_output_bytes = 4 * 1024 * 1024;
    std::map<std::string, std::string> env;
    std::string cwd;
};

struct ProcessResult {
    int exit_code = -1;
    bool timed_out = false;
    bool output_truncated = false;
    std::string output;  // stdout and stderr, merged
    std::string error;   // launch or I/O failure, not the child's diagnostics
};

ProcessResult run_process(const std::vector<std::string>& argv,
                          const ProcessOptions& options = {});

}  // namespace chudware::util
