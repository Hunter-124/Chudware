#include "security/containment.h"

#include <algorithm>
#include <cstring>
#include <limits>
#include <set>

#include <nlohmann/json.hpp>

#include "util/secret_store.h"
extern "C" {
#include "util/tweetnacl.h"
}

namespace chudware::security {
namespace {
using json = nlohmann::json;

int decodeChar(unsigned char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '-') return 62;
    if (c == '_') return 63;
    return -1;
}

bool decodeBase64Url(std::string_view input, std::vector<unsigned char>& out) {
    out.clear();
    if (input.find('=') != std::string_view::npos || input.size() % 4 == 1) return false;
    unsigned int acc = 0;
    int bits = 0;
    for (unsigned char c : input) {
        const int value = decodeChar(c);
        if (value < 0) return false;
        acc = (acc << 6) | static_cast<unsigned int>(value);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out.push_back(static_cast<unsigned char>((acc >> bits) & 0xff));
        }
    }
    return !bits || (acc & ((1u << bits) - 1u)) == 0;
}

bool verifySignature(std::string_view canonical, std::string_view encodedSignature,
                     const std::array<unsigned char, entitlement::kPublicKeyBytes>& publicKey) {
    std::vector<unsigned char> signature;
    if (!decodeBase64Url(encodedSignature, signature) || signature.size() != crypto_sign_BYTES ||
        canonical.size() > std::numeric_limits<std::size_t>::max() - signature.size())
        return false;
    std::vector<unsigned char> signedMessage(signature);
    signedMessage.insert(signedMessage.end(), canonical.begin(), canonical.end());
    std::vector<unsigned char> opened(signedMessage.size());
    unsigned long long openedLength = 0;
    return crypto_sign_open(opened.data(), &openedLength, signedMessage.data(),
                            static_cast<unsigned long long>(signedMessage.size()),
                            publicKey.data()) == 0 &&
           openedLength == canonical.size() &&
           std::equal(opened.begin(), opened.begin() + static_cast<std::ptrdiff_t>(openedLength),
                      canonical.begin());
}

EnforcementDecision fail(EnforcementStatus status, std::string detail) {
    EnforcementDecision decision;
    decision.status = status;
    decision.detail = std::move(detail);
    return decision;
}

bool exactString(const json& value, const char* key, std::string& out) {
    const auto it = value.find(key);
    if (it == value.end() || !it->is_string()) return false;
    out = it->get<std::string>();
    return !out.empty();
}

bool exactInteger(const json& value, const char* key, std::int64_t& out) {
    const auto it = value.find(key);
    if (it == value.end() || !it->is_number_integer()) return false;
    out = it->get<std::int64_t>();
    return true;
}

bool approvedReason(std::string_view reason) {
    static constexpr std::string_view approved[] = {
        "manifest_signature_invalid", "protected_file_hash_mismatch",
        "protected_module_unlisted", "debugger_attached",
        "trace_environment_present", "entitlement_signature_invalid"};
    return std::find(std::begin(approved), std::end(approved), reason) != std::end(approved);
}
}  // namespace

EnforcementDecision verifyEnforcementState(
    std::string_view envelopeJson, std::string_view expectedSubject,
    std::int64_t nowWall,
    const std::array<unsigned char, entitlement::kPublicKeyBytes>& publicKey) {
    if (expectedSubject.empty()) return fail(EnforcementStatus::SubjectMismatch, "expected subject is empty");
    try {
        json envelope = json::parse(envelopeJson);
        static const std::set<std::string> fields = {
            "action_id", "expires_at", "issued_at", "nonce", "reason_codes",
            "signature", "state", "subject"};
        if (!envelope.is_object() || envelope.size() != fields.size())
            return fail(EnforcementStatus::Malformed, "enforcement state shape is invalid");
        for (auto it = envelope.begin(); it != envelope.end(); ++it)
            if (!fields.count(it.key()))
                return fail(EnforcementStatus::Malformed, "enforcement state has an unknown field");

        EnforcementRecord record;
        std::string state;
        if (!exactString(envelope, "subject", record.subject) ||
            !exactString(envelope, "state", state) ||
            !exactInteger(envelope, "issued_at", record.issuedAt) ||
            !exactInteger(envelope, "expires_at", record.expiresAt) ||
            !exactString(envelope, "action_id", record.actionId) ||
            !exactString(envelope, "nonce", record.nonce) ||
            !exactString(envelope, "signature", record.signature) ||
            !envelope["reason_codes"].is_array())
            return fail(EnforcementStatus::Malformed, "enforcement state values are invalid");
        if (state == "active") record.state = EnforcementState::Active;
        else if (state == "reauthorize") record.state = EnforcementState::Reauthorize;
        else if (state == "contained") record.state = EnforcementState::Contained;
        else return fail(EnforcementStatus::Malformed, "enforcement state value is invalid");
        for (const auto& value : envelope["reason_codes"]) {
            if (!value.is_string() || value.get<std::string>().empty() ||
                !approvedReason(value.get<std::string>()))
                return fail(EnforcementStatus::Malformed, "enforcement reason code is invalid");
            record.reasonCodes.push_back(value.get<std::string>());
        }
        if (record.state == EnforcementState::Active && !record.reasonCodes.empty())
            return fail(EnforcementStatus::Malformed, "active state cannot contain reason codes");
        if (record.state != EnforcementState::Active && record.reasonCodes.empty())
            return fail(EnforcementStatus::Malformed, "non-active state requires a reason code");
        if (record.issuedAt <= 0 || record.expiresAt <= record.issuedAt)
            return fail(EnforcementStatus::Malformed, "enforcement time bounds are invalid");

        envelope.erase("signature");
        if (!verifySignature(envelope.dump(), record.signature, publicKey))
            return fail(EnforcementStatus::InvalidSignature, "enforcement signature is invalid");
        if (record.subject != expectedSubject)
            return fail(EnforcementStatus::SubjectMismatch, "enforcement subject does not match");
        if (nowWall < record.issuedAt)
            return fail(EnforcementStatus::NotYetValid, "enforcement state is not yet valid");
        if (nowWall >= record.expiresAt)
            return fail(EnforcementStatus::Expired, "enforcement state has expired");

        EnforcementDecision decision;
        decision.record = std::move(record);
        if (decision.record.state == EnforcementState::Active)
            decision.status = EnforcementStatus::Active;
        else if (decision.record.state == EnforcementState::Reauthorize)
            decision.status = EnforcementStatus::Reauthorize;
        else
            decision.status = EnforcementStatus::Contained;
        return decision;
    } catch (const std::exception&) {
        return fail(EnforcementStatus::Malformed, "enforcement state is not valid JSON");
    }
}

EnforcementDecision verifyEmbeddedEnforcementState(
    std::string_view envelopeJson, std::string_view expectedSubject,
    std::int64_t nowWall) {
    return verifyEnforcementState(envelopeJson, expectedSubject, nowWall,
                                  entitlement::kEmbeddedPublicKey);
}

bool persistEnforcementState(std::string_view envelopeJson, std::string_view dataDir) {
    const std::vector<unsigned char> bytes(envelopeJson.begin(), envelopeJson.end());
    return secret_store::store(kContainmentService, kContainmentAccount, bytes, dataDir);
}

EnforcementDecision loadEnforcementState(
    std::string_view dataDir, std::string_view expectedSubject,
    std::int64_t nowWall,
    const std::array<unsigned char, entitlement::kPublicKeyBytes>& publicKey) {
    std::vector<unsigned char> bytes;
    if (!secret_store::load(kContainmentService, kContainmentAccount, bytes, dataDir))
        return fail(EnforcementStatus::Missing, "signed enforcement state is missing");
    return verifyEnforcementState(
        std::string_view(reinterpret_cast<const char*>(bytes.data()), bytes.size()),
        expectedSubject, nowWall, publicKey);
}

EnforcementDecision loadEmbeddedEnforcementState(
    std::string_view dataDir, std::string_view expectedSubject,
    std::int64_t nowWall) {
#if defined(CHUDWARE_DEVELOPER_BUILD)
    (void)dataDir;
    (void)expectedSubject;
    (void)nowWall;
    EnforcementDecision decision;
    decision.status = EnforcementStatus::DeveloperBypass;
    return decision;
#else
    return loadEnforcementState(dataDir, expectedSubject, nowWall,
                                entitlement::kEmbeddedPublicKey);
#endif
}

const char* enforcementStatusName(EnforcementStatus status) noexcept {
    switch (status) {
        case EnforcementStatus::Active: return "active";
        case EnforcementStatus::DeveloperBypass: return "developer_bypass";
        case EnforcementStatus::Missing: return "missing";
        case EnforcementStatus::Malformed: return "malformed";
        case EnforcementStatus::InvalidSignature: return "invalid_signature";
        case EnforcementStatus::SubjectMismatch: return "subject_mismatch";
        case EnforcementStatus::NotYetValid: return "not_yet_valid";
        case EnforcementStatus::Expired: return "expired";
        case EnforcementStatus::Reauthorize: return "reauthorize";
        case EnforcementStatus::Contained: return "contained";
        case EnforcementStatus::StoreFailed: return "store_failed";
    }
    return "malformed";
}

}  // namespace chudware::security
