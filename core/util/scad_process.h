#pragma once

#include <chrono>
#include <cstddef>
#include <string>
#include <vector>

namespace chudware::util::scad_process {

struct Request {
    std::string executable;
    std::vector<std::string> arguments;
    std::string work_dir;
    // A single relative filename, or empty for commands such as --version.
    std::string output_file;
    std::vector<std::string> renderer_resource_roots;
    std::chrono::milliseconds deadline{60000};
    std::size_t max_diagnostic_bytes = 65536;
    std::size_t max_output_bytes = 128 * 1024 * 1024;
};

enum class Status {
    success, dependency_unavailable, sandbox_unavailable, model_failure,
    timed_out, resource_limit, invalid_request
};

struct Result {
    Status status;
    int exit_code = -1;
    std::string diagnostics;
};

// Executes --version inside the same confinement used for rendering.
Result capability(const std::string& executable);
// Runs literal arguments in a private copy of work_dir. Only a successful,
// bounded regular output is atomically published into the original work_dir.
Result run(const Request& request);

}  // namespace chudware::util::scad_process
