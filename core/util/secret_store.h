#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace chudware::secret_store {

// Three-function platform seam. On Windows, `dataDir` stores only user-bound
// DPAPI ciphertext; POSIX mode bits are not its protection. On macOS and Linux,
// `dataDir` is used only for the explicit, verified-0600 fallback when the OS
// keychain is unavailable.
bool store(std::string_view service, std::string_view account,
           const std::vector<unsigned char>& value, std::string_view dataDir);
bool load(std::string_view service, std::string_view account,
          std::vector<unsigned char>& value, std::string_view dataDir);
bool erase(std::string_view service, std::string_view account,
           std::string_view dataDir);

}  // namespace chudware::secret_store
