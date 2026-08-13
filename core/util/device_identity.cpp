#include "util/device_identity.h"

#include <array>
#include <charconv>
#include <filesystem>
#include <fstream>
#include <string>
#include <mutex>
#include <vector>

#include <nlohmann/json.hpp>

#include "util/secret_store.h"
extern "C" {
#include "util/tweetnacl.h"
}

namespace chudware::device_identity {
namespace {

using json = nlohmann::json;
std::mutex sequenceMutex;
constexpr std::string_view kSequenceAccount = "device-refresh-sequence-v1";
constexpr std::string_view kPendingAccount = "device-signing-pending-v1";

std::string base64url(const unsigned char* data, std::size_t size) {
    static constexpr char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    std::string out;
    out.reserve((size * 4 + 2) / 3);
    unsigned int acc = 0;
    int bits = 0;
    for (std::size_t i = 0; i < size; ++i) {
        acc = (acc << 8) | data[i];
        bits += 8;
        while (bits >= 6) {
            bits -= 6;
            out.push_back(alphabet[(acc >> bits) & 63]);
        }
    }
    if (bits) out.push_back(alphabet[(acc << (6 - bits)) & 63]);
    return out;
}

bool validSecret(const std::vector<unsigned char>& secret,
                 std::array<unsigned char, crypto_sign_PUBLICKEYBYTES>& publicKey) {
    if (secret.size() != crypto_sign_SECRETKEYBYTES) return false;
    std::copy_n(secret.data() + 32, publicKey.size(), publicKey.data());
    static constexpr unsigned char probe[] = "chudware-device-signing-v1";
    std::array<unsigned char, crypto_sign_BYTES + sizeof(probe) - 1> signedProbe{};
    unsigned long long signedLength = 0;
    if (crypto_sign(signedProbe.data(), &signedLength, probe, sizeof(probe) - 1,
                    secret.data()) != 0)
        return false;
    // TweetNaCl writes up to the signed-message length even on verification
    // failure, so allocate that full bound rather than only the plaintext.
    std::array<unsigned char, crypto_sign_BYTES + sizeof(probe) - 1> opened{};
    unsigned long long openedLength = 0;
    return crypto_sign_open(opened.data(), &openedLength, signedProbe.data(), signedLength,
                            publicKey.data()) == 0 && openedLength == sizeof(probe) - 1 &&
           std::equal(probe, probe + sizeof(probe) - 1, opened.begin());
}

Identity identityFromSecret(const std::vector<unsigned char>& secret) {
    std::array<unsigned char, crypto_sign_PUBLICKEYBYTES> publicKey{};
    if (!validSecret(secret, publicKey)) return {KeyStatus::Corrupt, {}};
    return {KeyStatus::Ready, base64url(publicKey.data(), publicKey.size())};
}

bool exactUnsignedEnvelope(const json& envelope) {
    static constexpr std::array<std::string_view, 8> required = {
        "token", "request_nonce", "sequence", "app_version", "platform",
        "integrity_digest", "last_acked_event_sequence", "customer_receipt"};
    if (!envelope.is_object() || envelope.size() != required.size()) return false;
    for (std::string_view key : required)
        if (!envelope.contains(std::string(key))) return false;
    return envelope["token"].is_string() && envelope["request_nonce"].is_string() &&
           envelope["sequence"].is_number_unsigned() && envelope["app_version"].is_string() &&
           envelope["platform"].is_string() && envelope["integrity_digest"].is_string() &&
           envelope["last_acked_event_sequence"].is_number_unsigned() &&
           (envelope["customer_receipt"].is_null() || envelope["customer_receipt"].is_object());
}

bool exactUnsignedSecurityEnvelope(const json& envelope) {
    static constexpr std::array<std::string_view, 7> required = {
        "batch_id", "subject", "sequence", "policy_id", "app_version", "platform", "events"};
    if (!envelope.is_object() || envelope.size() != required.size()) return false;
    for (std::string_view key : required)
        if (!envelope.contains(std::string(key))) return false;
    return envelope["batch_id"].is_string() && envelope["subject"].is_string() &&
           envelope["sequence"].is_number_unsigned() && envelope["policy_id"].is_string() &&
           envelope["app_version"].is_string() && envelope["platform"].is_string() &&
           envelope["events"].is_array();
}

bool exactUnsignedCustomerReceipt(const json& receipt) {
    static constexpr std::array<std::string_view, 6> required = {
        "subject", "sequence", "request_nonce", "verdict", "reason", "issued_at"};
    if (!receipt.is_object() || receipt.size() != required.size()) return false;
    for (std::string_view key : required)
        if (!receipt.contains(std::string(key))) return false;
    return receipt["subject"].is_string() && receipt["sequence"].is_number_unsigned() &&
           receipt["request_nonce"].is_string() && receipt["verdict"] == "allow" &&
           receipt["reason"] == "heartbeat" && receipt["issued_at"].is_number_integer();
}

bool signExactEnvelope(json& envelope, std::string_view dataDir, std::string* error) {
    std::vector<unsigned char> secret;
    if (!secret_store::load(kService, kAccount, secret, dataDir)) {
        if (error) *error = "device signing key is missing";
        return false;
    }
    if (identityFromSecret(secret).status != KeyStatus::Ready) {
        if (error) *error = "device signing key is corrupt";
        return false;
    }
    const std::string canonical = envelope.dump();
    std::vector<unsigned char> signedMessage(crypto_sign_BYTES + canonical.size());
    unsigned long long signedLength = 0;
    if (crypto_sign(signedMessage.data(), &signedLength,
                    reinterpret_cast<const unsigned char*>(canonical.data()), canonical.size(),
                    secret.data()) != 0 || signedLength != signedMessage.size()) {
        if (error) *error = "device signing failed";
        return false;
    }
    envelope["signature"] = base64url(signedMessage.data(), crypto_sign_BYTES);
    return true;
}

std::filesystem::path sequencePath(std::string_view dataDir) {
    return std::filesystem::path(dataDir) / "secrets" /
           "com_chudware_desktop-device-refresh-sequence-v1.state";
}

}  // namespace

Identity load(std::string_view dataDir) {
    std::vector<unsigned char> secret;
    if (!secret_store::load(kService, kAccount, secret, dataDir))
        return {KeyStatus::Missing, {}};
    return identityFromSecret(secret);
}

Identity ensureForAuthorization(std::string_view dataDir) {
    Identity existing = load(dataDir);
    if (existing.status == KeyStatus::Ready) return existing;
    if (existing.status == KeyStatus::Corrupt &&
        !secret_store::erase(kService, kAccount, dataDir))
        return {KeyStatus::StoreFailed, {}};

    std::array<unsigned char, crypto_sign_PUBLICKEYBYTES> publicKey{};
    std::vector<unsigned char> secret(crypto_sign_SECRETKEYBYTES);
    if (crypto_sign_keypair(publicKey.data(), secret.data()) != 0)
        return {KeyStatus::StoreFailed, {}};
    if (!secret_store::store(kService, kAccount, secret, dataDir))
        return {KeyStatus::StoreFailed, {}};
    return {KeyStatus::Ready, base64url(publicKey.data(), publicKey.size())};
}

Identity prepareAuthorizationKey(std::string_view dataDir) {
    std::array<unsigned char, crypto_sign_PUBLICKEYBYTES> publicKey{};
    std::vector<unsigned char> secret(crypto_sign_SECRETKEYBYTES);
    if (crypto_sign_keypair(publicKey.data(), secret.data()) != 0 ||
        !secret_store::store(kService, kPendingAccount, secret, dataDir))
        return {KeyStatus::StoreFailed, {}};
    return {KeyStatus::Ready, base64url(publicKey.data(), publicKey.size())};
}

bool finalizeAuthorizationKey(std::string_view dataDir) {
    std::vector<unsigned char> pending;
    if (!secret_store::load(kService, kPendingAccount, pending, dataDir) ||
        identityFromSecret(pending).status != KeyStatus::Ready ||
        !secret_store::store(kService, kAccount, pending, dataDir))
        return false;
    return secret_store::erase(kService, kPendingAccount, dataDir);
}

void discardAuthorizationKey(std::string_view dataDir) {
    (void)secret_store::erase(kService, kPendingAccount, dataDir);
}

bool signRefreshEnvelope(json& envelope, std::string_view dataDir, std::string* error) {
    if (!exactUnsignedEnvelope(envelope)) {
        if (error) *error = "refresh envelope fields are not canonical";
        return false;
    }
    return signExactEnvelope(envelope, dataDir, error);
}

bool signSecurityEnvelope(json& envelope, std::string_view dataDir, std::string* error) {
    if (!exactUnsignedSecurityEnvelope(envelope)) {
        if (error) *error = "security envelope fields are not canonical";
        return false;
    }
    return signExactEnvelope(envelope, dataDir, error);
}

bool signCustomerReceipt(json& receipt, std::string_view dataDir, std::string* error) {
    if (!exactUnsignedCustomerReceipt(receipt)) {
        if (error) *error = "customer receipt fields are not canonical";
        return false;
    }
    return signExactEnvelope(receipt, dataDir, error);
}

std::uint64_t nextRefreshSequence(std::string_view dataDir) {
    std::lock_guard<std::mutex> lock(sequenceMutex);
    const auto path = sequencePath(dataDir);
    std::uint64_t current = 0;
    {
        std::ifstream input(path, std::ios::binary);
        std::string text;
        if (input) std::getline(input, text);
        if (!text.empty()) {
            const auto result = std::from_chars(text.data(), text.data() + text.size(), current);
            if (result.ec != std::errc{} || result.ptr != text.data() + text.size()) current = 0;
        }
    }
    if (current == UINT64_MAX) return current;
    ++current;
    std::error_code ec;
    std::filesystem::create_directories(path.parent_path(), ec);
    if (ec) return 0;
    const auto temporary = path.string() + ".tmp";
    {
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        output << current << '\n';
        if (!output) return 0;
    }
#if !defined(_WIN32)
    std::filesystem::permissions(temporary, std::filesystem::perms::owner_read |
        std::filesystem::perms::owner_write, std::filesystem::perm_options::replace, ec);
    if (ec) return 0;
#endif
    std::filesystem::rename(temporary, path, ec);
    if (ec) {
        std::filesystem::remove(path, ec);
        ec.clear();
        std::filesystem::rename(temporary, path, ec);
    }
    return ec ? 0 : current;
}

}  // namespace chudware::device_identity
