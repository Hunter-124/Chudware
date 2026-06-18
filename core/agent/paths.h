// core/agent/paths.h — C++ port of engine/paths.py (canonical filesystem layout).
//
// P52: the agent SUPPORT layer needs the same %APPDATA%/AI-CAD anchor the Python
// engine uses so events.jsonl + prompt overrides land in the identical place.
//   appdata_root() = (env APPDATA or ~/AppData/Roaming) / "AI-CAD".
//   ROOT()  = appdata_root();  CACHE() = ROOT/"cache";  CONFIG() = ROOT/"config.json".
// Exposed as functions (not constants) because APPDATA can change per-process in
// the parity gate; the Python module captures it at import, but reading it live
// here is byte-identical for any fixed environment.
#pragma once

#include <filesystem>

namespace aicad {
namespace agent {

std::filesystem::path appdata_root();
std::filesystem::path paths_root();
std::filesystem::path paths_cache();
std::filesystem::path paths_config();

}  // namespace agent
}  // namespace aicad
