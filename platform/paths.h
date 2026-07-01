// Cross-OS path helpers. Most callers append "/AI-CAD/..." to appdataDir().
#pragma once

#include <string>

namespace platform {

std::string exeDir();

// Parent of the AI-CAD data dir (Windows: %APPDATA%; Linux: $XDG_DATA_HOME or ~/.local/share).
std::string appdataDir();

// Alias kept for early scaffold call sites.
inline std::string userDataDir() { return appdataDir(); }

// Full AI-CAD data root (forward slashes — valid on Windows too).
std::string appdataRoot();
inline std::string appDataRoot() { return appdataRoot(); }

std::string configPath();
std::string cacheDir();
std::string tempDir();
std::string imguiIniPath();
std::string documentsDir();

// Bundled runtime assets beside the exe (AI-CAD2: resources/; host: assets/).
std::string resourcesDir();
std::string assetsDir();

std::string joinPath(const std::string& a, const std::string& b);

// Human-readable label for settings UI (not necessarily the resolved path).
std::string appdataDisplayLabel();

}  // namespace platform
