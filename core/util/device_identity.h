#pragma once

#include <cstdint>
#include <string>
#include <string_view>

#include <nlohmann/json_fwd.hpp>

namespace chudware::device_identity {

inline constexpr std::string_view kService = "com.chudware.desktop";
inline constexpr std::string_view kAccount = "device-signing-v1";

enum class KeyStatus { Ready, Missing, Corrupt, StoreFailed };

struct Identity {
    KeyStatus status = KeyStatus::Missing;
    std::string publicKey;
};

// Loads the existing Ed25519 identity. Missing/corrupt identities are never
// replaced here: callers must send the user through explicit browser
// authorization before rotating identity.
Identity load(std::string_view dataDir);

// Used only at the explicit browser-authorization boundary. Generates and
// persists a key exactly when no valid identity exists, then returns its
// unpadded base64url public key.
Identity ensureForAuthorization(std::string_view dataDir);

// Creates a separate authorization candidate without replacing the current key.
Identity prepareAuthorizationKey(std::string_view dataDir);
// Promotes the prepared candidate only after the browser response is verified.
bool finalizeAuthorizationKey(std::string_view dataDir);
void discardAuthorizationKey(std::string_view dataDir);

// Adds `signature` to the exact canonical refresh envelope. The input must
// contain every required field except signature and no additional fields.
bool signRefreshEnvelope(nlohmann::json& envelope, std::string_view dataDir,
                         std::string* error = nullptr);

// Adds `signature` to the exact canonical security-event batch envelope.
bool signSecurityEnvelope(nlohmann::json& envelope, std::string_view dataDir,
                          std::string* error = nullptr);

// Adds `signature` to the exact refresh-bound customer acceptance receipt.
bool signCustomerReceipt(nlohmann::json& receipt, std::string_view dataDir,
                         std::string* error = nullptr);

// Monotonic per-install refresh sequence stored separately from the private
// key. A process restart never resets it.
std::uint64_t nextRefreshSequence(std::string_view dataDir);

}  // namespace chudware::device_identity
