#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <nlohmann/json_fwd.hpp>

namespace chudware::entitlement {

inline constexpr std::size_t kPublicKeyBytes = 32;
inline constexpr std::array<unsigned char, kPublicKeyBytes> kEmbeddedPublicKey = {
    0xbd, 0xb8, 0x86, 0x90, 0x38, 0xe8, 0x27, 0x4a,
    0x32, 0xa6, 0xf1, 0x4b, 0x86, 0xf0, 0xfb, 0xdb,
    0xe5, 0x99, 0xc6, 0xc7, 0x8e, 0x49, 0xdc, 0x49,
    0x5c, 0xfa, 0x12, 0x75, 0xb0, 0x14, 0x90, 0xf3,
};

struct TokenPayload {
    int version = 0;
    std::string subject;
    std::string email;
    std::string plan;
    std::int64_t issuedAt = 0;
    std::int64_t paidThrough = 0;
    std::int64_t subscribedSince = 0;
    int graceDays = 0;
    std::string nonce;
};

struct CheckinStamp {
    std::int64_t wallSeconds = 0;
    double monotonicSeconds = 0.0;
    std::string bootId;
};

enum class OfflineAccess {
    Allowed,
    Expiring,
    Expired,
    ClockRollback,
};

struct OfflineDecision {
    OfflineAccess access = OfflineAccess::Expired;
    std::int64_t deadline = 0;
    std::int64_t secondsRemaining = 0;
};

enum class RefreshOutcome { Fresh, Refused, Unreachable };
RefreshOutcome classifyRefresh(long httpStatus, bool hasVerifiedFreshToken,
                               bool hasVerifiedSignedRefusal);

// Strict compact-token verification. The signed message is the base64url payload
// exactly as received; JWT parsing and algorithm negotiation are deliberately absent.
bool verifyToken(std::string_view token,
                 const std::array<unsigned char, kPublicKeyBytes>& publicKey,
                 TokenPayload& payload,
                 std::string* error = nullptr);
bool verifyEmbeddedToken(std::string_view token, TokenPayload& payload,
                         std::string* error = nullptr);

// A refusal signs the canonical JSON object stored in `proof` and is accepted only
// when request_nonce matches the freshly generated, one-use refresh challenge.
// This remains replay-safe even when the machine's wall clock is wrong.
bool verifyRefusal(std::string_view payloadJson,
                   const std::array<unsigned char, kPublicKeyBytes>& publicKey,
                   std::string_view expectedRequestNonce,
                   std::int64_t nowWall,
                   std::string* reason = nullptr,
                   std::string* error = nullptr);

// Strict signed server-policy verification. The endpoint is accepted only from
// the authenticated policy object and only for customer_endpoint mode.
struct SecurityPolicy {
    std::string id;
    std::string subject;
    std::string customerEndpoint;
    std::int64_t issuedAt = 0;
    std::int64_t expiresAt = 0;
};
bool verifySecurityPolicy(std::string_view policyJson,
                          const std::array<unsigned char, kPublicKeyBytes>& publicKey,
                          std::string_view expectedSubject,
                          SecurityPolicy& policy,
                          std::string* error = nullptr);
bool verifyEmbeddedSecurityPolicy(std::string_view policyJson,
                                  std::string_view expectedSubject,
                                  SecurityPolicy& policy,
                                  std::string* error = nullptr);
std::int64_t graceDeadline(const TokenPayload& payload,
                           std::int64_t lastSuccessfulCheckinWall);
OfflineDecision offlineAccess(const TokenPayload& payload,
                              const CheckinStamp& lastSuccessfulCheckin,
                              std::int64_t nowWall,
                              double nowMonotonic,
                              std::string_view currentBootId);

// XSalsa20-Poly1305 (TweetNaCl secretbox). The key must be a stable,
// device-derived 32-byte digest. This is a speed bump against casual copying,
// not an anti-tamper claim.
std::vector<unsigned char> sealForDevice(std::string_view plaintext,
                                         const std::array<unsigned char, 32>& deviceKey);
bool openForDevice(const std::vector<unsigned char>& sealed,
                   const std::array<unsigned char, 32>& deviceKey,
                   std::string& plaintext);
std::array<unsigned char, 32> deviceKey(std::string_view stableDeviceMaterial);

}  // namespace chudware::entitlement
