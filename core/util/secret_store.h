#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace chudware::secret_store {

// Three-function platform seam. `dataDir` is used only for DPAPI ciphertext and
// for the explicit 0600 fallback when no OS keychain is available.
bool store(std::string_view service, std::string_view account,
           const std::vector<unsigned char>& value, std::string_view dataDir);
bool load(std::string_view service, std::string_view account,
          std::vector<unsigned char>& value, std::string_view dataDir);
bool erase(std::string_view service, std::string_view account,
           std::string_view dataDir);

}  // namespace chudware::secret_store
