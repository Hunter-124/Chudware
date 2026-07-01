// CAD-specific file dialog wrappers over platform/dialogs.h.
#pragma once

#include <string>

namespace platform {

std::string openCadFile();
std::string saveCadFile(const std::string& defaultName);

}  // namespace platform
