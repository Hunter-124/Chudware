// Subprocess + PATH-resolution helpers for the firmware toolchain wrappers.
//
// Every firmware backend drives an unmodified external CLI as a subprocess
// (contracts/firmware_rpc.md §1) — this is the C++ port of the subprocess
// plumbing shared by engine_oracle/firmware/toolchains.py / hardware.py /
// provision.py. Never raises: a missing executable or a timeout maps to
// returncode -1, exactly like the Python `_run` helpers.
#pragma once

#include <string>
#include <cstddef>
#include <vector>

namespace chudware::firmware {

struct RunResult {
    int returncode = -1;  // -1 = missing tool / timeout / output refusal
    std::string stdout_text;
    std::string stderr_text;
    bool timed_out = false;
    bool not_found = false;
    bool output_limit_exceeded = false;
};

// Resolve an executable name against PATH (shutil.which equivalent). Returns
// the absolute path, or "" when absent. Never throws.
std::string which(const std::string& name);

// Run `argv` (argv[0] is the program) with stdout+stderr captured, in `cwd`
// (empty = current process cwd), with a wall-clock timeout in seconds
// (nonpositive = no deadline). Capture is bounded before every append: at most
// output_limit bytes combined and output_limit/2 bytes per stream (raw bytes,
// before universal-newline normalization).
// Never throws. On a missing executable: returncode -1, not_found=true.
// On timeout: the process group/tree is killed, returncode -1, timed_out=true.
// On excess output: the group/tree is killed, returncode -1,
// output_limit_exceeded=true; stderr includes a marker within the same budget.
// minimal_env passes only PATH (and Windows SystemRoot), HOME and TMPDIR;
// HOME/TMPDIR are the absolute cwd. Credentials/operator environment are absent.
RunResult run_capture(const std::vector<std::string>& argv, const std::string& cwd,
                      double timeout_s, bool minimal_env = false,
                      std::size_t output_limit = 1024 * 1024);

}  // namespace chudware::firmware
