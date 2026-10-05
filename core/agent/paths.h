// core/agent/paths.h — the C++ core's per-user data root (events.jsonl, prompt
// overrides, config.json, cache). Counterpart of services/paths.py active_root() and
// the GUI's host/src/core/app_state.cpp appdataAppDir(); all three must pick one dir.
//   base: Windows %APPDATA% (else %USERPROFILE%\AppData\Roaming);
//         elsewhere $XDG_DATA_HOME (else ~/.local/share).
//   appdata_root() = base/"Chudware". When that has no config.json but a pre-rename
//     root (base/"Billet", then base/"AI-CAD") does, the legacy contents are copied
//     forward (copy-if-missing; venvs and re-provisionable payloads skipped; the old dir
//     is never moved or deleted). Only a FAILED copy returns the legacy root instead.
//   paths_root() = appdata_root();  paths_cache() = ROOT/"cache";
//   paths_config() = ROOT/"config.json".
// Functions, not constants: the environment is read on every call so tests can
// redirect it per process. Unlike services/paths.py, APPDATA is not consulted on POSIX.
#pragma once

#include <filesystem>

namespace chudware {
namespace agent {

std::filesystem::path appdata_root();
std::filesystem::path paths_root();
std::filesystem::path paths_cache();
std::filesystem::path paths_config();

}  // namespace agent
}  // namespace chudware
