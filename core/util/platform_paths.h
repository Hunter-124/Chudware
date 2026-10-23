#pragma once
// Process-identity primitives, one spelling per OS.
//
// Six hand-rolled copies of readlink("/proc/self/exe") used to live across
// core/, host/ and services/. Every one of them silently returned nothing on
// Darwin (there is no /proc there) and three of them re-implemented the same
// UTF-16 -> UTF-8 conversion on Windows. Route every call site through here.
//
// All functions are pure queries: they never throw, never log, and report
// failure by value ("" / false / empty path).

#include <filesystem>
#include <string>

namespace chudware::platform {

// Absolute path of the running executable, or "" when it cannot be determined.
//   Linux    readlink("/proc/self/exe")
//   Darwin   _NSGetExecutablePath() + realpath() (the raw value may be relative
//            and may contain symlinks/"..", which /proc/self/exe never is)
//   Windows  GetModuleFileNameW() transcoded to UTF-8
//   Wasm     always "" — a browser tab has no executable image
std::string executablePath();

// True while `pid` names a live, non-zombie process. pid <= 1 is always false:
// no caller means init, and every caller here is asking about a peer it spawned
// or recorded. A pid owned by another user counts as alive (kill(2) reports
// EPERM, which proves existence) so a recycled pid is never mistaken for a free
// slot. Zombies are excluded — that is what the /proc/<pid>/stat state check
// bought on Linux and what sysctl(KERN_PROC_PID) buys on Darwin.
bool processAlive(long pid);

// Developer-build source checkout: the nearest ancestor of the executable's
// directory, then of the current directory, that contains
// services/worker_main.py. Empty when neither lies inside a checkout (always
// empty in the browser).
std::filesystem::path developerSourceRoot();

}  // namespace chudware::platform
