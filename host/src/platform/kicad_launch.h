#pragma once

#include <string>

namespace platform {

// Launch KiCad without a shell. The child outlives the host; unavailable on web.
bool launchKicad(const std::string& executable, const std::string& document);

}  // namespace platform
