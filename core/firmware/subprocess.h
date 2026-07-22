// Subprocess + PATH-resolution helpers for the firmware toolchain wrappers.
//
// Every firmware backend drives an unmodified external CLI as a subprocess
// (contracts/firmware_rpc.md §1) — this is the C++ port of the subprocess
// plumbing shared by engine_oracle/firmware/toolchains.py / hardware.py /
// provision.py. Never raises: a missing executable or a timeout maps to
// returncode -1, exactly like the Python `_run` helpers.
#pragma once

#include <string>
#include <vector>

namespace aicad::firmware {

struct RunResult {
    int returncode = -1;  // -1 = tool not found / timeout (Python parity)
    std::string stdout_text;
    std::string stderr_text;
    bool timed_out = false;
    bool not_found = false;
};

// Resolve an executable name against PATH (shutil.which equivalent). Returns
// the absolute path, or "" when absent. Never throws.
std::string which(const std::string& name);

// Run `argv` (argv[0] is the program) with stdout+stderr captured, in `cwd`
// (empty = current process cwd), with a wall-clock timeout in seconds.
// Never throws. On a missing executable: returncode -1, not_found=true.
// On timeout: the child is killed, returncode -1, timed_out=true.
RunResult run_capture(const std::vector<std::string>& argv, const std::string& cwd,
                      double timeout_s);

}  // namespace aicad::firmware
