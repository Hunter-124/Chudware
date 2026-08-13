#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "util/entitlement.h"

namespace chudware::security {

inline constexpr std::string_view kContainmentService = "com.chudware.desktop";
inline constexpr std::string_view kContainmentAccount = "security-state-v1";

enum class EnforcementState { Active, Reauthorize, Contained };

enum class EnforcementStatus {
    Active,
    DeveloperBypass,
    Missing,
    Malformed,
    InvalidSignature,
    SubjectMismatch,
    NotYetValid,
    Expired,
    Reauthorize,
    Contained,
    StoreFailed,
};

struct EnforcementRecord {
    std::string subject;
    EnforcementState state = EnforcementState::Contained;
    std::int64_t issuedAt = 0;
    std::int64_t expiresAt = 0;
    std::vector<std::string> reasonCodes;
    std::string actionId;
    std::string nonce;
    std::string signature;
};

struct EnforcementDecision {
    EnforcementStatus status = EnforcementStatus::Missing;
    EnforcementRecord record;
    std::string detail;

    bool active() const noexcept {
        return status == EnforcementStatus::Active ||
               status == EnforcementStatus::DeveloperBypass;
    }
};

// Verifies an exact eight-field canonical JSON envelope. The signature is
// Ed25519 over the canonical JSON object after removing `signature`.
EnforcementDecision verifyEnforcementState(
    std::string_view envelopeJson, std::string_view expectedSubject,
    std::int64_t nowWall,
    const std::array<unsigned char, entitlement::kPublicKeyBytes>& publicKey);
EnforcementDecision verifyEmbeddedEnforcementState(
    std::string_view envelopeJson, std::string_view expectedSubject,
    std::int64_t nowWall);

// Persists and reloads the signed envelope without introducing any local
// override. Loading always performs signature, subject, and time verification.
bool persistEnforcementState(std::string_view envelopeJson,
                             std::string_view dataDir);
EnforcementDecision loadEnforcementState(
    std::string_view dataDir, std::string_view expectedSubject,
    std::int64_t nowWall,
    const std::array<unsigned char, entitlement::kPublicKeyBytes>& publicKey);
EnforcementDecision loadEmbeddedEnforcementState(
    std::string_view dataDir, std::string_view expectedSubject,
    std::int64_t nowWall);

constexpr bool containmentDeveloperBypass() noexcept {
#if defined(CHUDWARE_DEVELOPER_BUILD)
    return true;
#else
    return false;
#endif
}

const char* enforcementStatusName(EnforcementStatus status) noexcept;

}  // namespace chudware::security
