#include "util/entitlement.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <limits>
#include <set>
#include <stdexcept>

#include <nlohmann/json.hpp>

extern "C" {
#include "util/tweetnacl.h"
}

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <bcrypt.h>
#elif defined(__APPLE__)
#  include <Security/Security.h>
#else
#  include <cerrno>
#  include <fcntl.h>
#  include <sys/random.h>
#  include <unistd.h>
#endif

namespace {

using json = nlohmann::json;

void setError(std::string* error, std::string text) {
    if (error) *error = std::move(text);
}

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
        const int v = decodeChar(c);
        if (v < 0) return false;
        acc = (acc << 6) | static_cast<unsigned int>(v);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out.push_back(static_cast<unsigned char>((acc >> bits) & 0xff));
        }
    }
    if (bits && (acc & ((1u << bits) - 1u)) != 0) return false;  // non-canonical tail
    return true;
}

bool verifyDetached(std::string_view encodedPayload, std::string_view encodedSignature,
                    const std::array<unsigned char, 32>& publicKey,
                    std::string& decodedPayload) {
    std::vector<unsigned char> signature;
    std::vector<unsigned char> payload;
    if (!decodeBase64Url(encodedSignature, signature) || signature.size() != crypto_sign_BYTES ||
        !decodeBase64Url(encodedPayload, payload))
        return false;
    if (encodedPayload.size() > 64 * 1024 ||
        encodedPayload.size() > std::numeric_limits<std::size_t>::max() - signature.size())
        return false;

    std::vector<unsigned char> signedMessage(signature.size() + encodedPayload.size());
    std::memcpy(signedMessage.data(), signature.data(), signature.size());
    std::memcpy(signedMessage.data() + signature.size(), encodedPayload.data(), encodedPayload.size());
    std::vector<unsigned char> opened(signedMessage.size());
    unsigned long long openedLength = 0;
    if (crypto_sign_open(opened.data(), &openedLength, signedMessage.data(),
                         static_cast<unsigned long long>(signedMessage.size()),
                         publicKey.data()) != 0 ||
        openedLength != encodedPayload.size() ||
        !std::equal(opened.begin(), opened.begin() + static_cast<std::ptrdiff_t>(openedLength),
                    encodedPayload.begin()))
        return false;
    decodedPayload.assign(reinterpret_cast<const char*>(payload.data()), payload.size());
    return true;
}

bool splitCompact(std::string_view token, std::string_view& payload, std::string_view& signature) {
    const std::size_t dot = token.find('.');
    if (dot == std::string_view::npos || dot == 0 || dot + 1 >= token.size() ||
        token.find('.', dot + 1) != std::string_view::npos)
        return false;
    payload = token.substr(0, dot);
    signature = token.substr(dot + 1);
    return true;
}

bool exactInteger(const json& value, const char* name, std::int64_t& out) {
    const auto it = value.find(name);
    if (it == value.end() || !it->is_number_integer()) return false;
    out = it->get<std::int64_t>();
    return true;
}

bool exactString(const json& value, const char* name, std::string& out) {
    const auto it = value.find(name);
    if (it == value.end() || !it->is_string()) return false;
    out = it->get<std::string>();
    return !out.empty();
}

}  // namespace

extern "C" void randombytes(unsigned char* out, unsigned long long length) {
#if defined(_WIN32)
    if (BCryptGenRandom(nullptr, out, static_cast<ULONG>(length),
                        BCRYPT_USE_SYSTEM_PREFERRED_RNG) == 0)
        return;
#elif defined(__APPLE__)
    if (SecRandomCopyBytes(kSecRandomDefault, static_cast<size_t>(length), out) == errSecSuccess)
        return;
#else
    unsigned long long done = 0;
    while (done < length) {
        const ssize_t n = ::getrandom(out + done, static_cast<size_t>(length - done), 0);
        if (n > 0) {
            done += static_cast<unsigned long long>(n);
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        break;
    }
    if (done == length) return;
    const int fd = ::open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    if (fd >= 0) {
        while (done < length) {
            const ssize_t n = ::read(fd, out + done, static_cast<size_t>(length - done));
            if (n > 0) done += static_cast<unsigned long long>(n);
            else if (n < 0 && errno == EINTR) continue;
            else break;
        }
        ::close(fd);
        if (done == length) return;
    }
#endif
    std::terminate();  // never produce predictable crypto material
}

namespace chudware::entitlement {

RefreshOutcome classifyRefresh(long httpStatus, bool hasVerifiedFreshToken,
                               bool hasVerifiedSignedRefusal) {
    if (httpStatus == 200 && hasVerifiedFreshToken) return RefreshOutcome::Fresh;
    if (httpStatus == 200 && hasVerifiedSignedRefusal) return RefreshOutcome::Refused;
    // Includes transport status 0, every 4xx (even 401), and every 5xx. None is
    // an authoritative denial without the signed 200 refusal body.
    return RefreshOutcome::Unreachable;
}

bool verifyToken(std::string_view token,
                 const std::array<unsigned char, kPublicKeyBytes>& publicKey,
                 TokenPayload& payload, std::string* error) {
    std::string_view encodedPayload;
    std::string_view encodedSignature;
    if (!splitCompact(token, encodedPayload, encodedSignature)) {
        setError(error, "malformed entitlement token");
        return false;
    }
    std::string decoded;
    if (!verifyDetached(encodedPayload, encodedSignature, publicKey, decoded)) {
        setError(error, "invalid entitlement signature");
        return false;
    }

    try {
        const json value = json::parse(decoded);
        static const std::set<std::string> expected = {
            "email", "grace_days", "issued_at", "nonce", "paid_through", "plan",
            "sub", "subscribed_since", "v"};
        if (!value.is_object() || value.size() != expected.size()) {
            setError(error, "entitlement payload shape is invalid");
            return false;
        }
        for (auto it = value.begin(); it != value.end(); ++it) {
            if (!expected.count(it.key())) {
                setError(error, "entitlement payload contains an unknown field");
                return false;
            }
        }
        std::int64_t version = 0;
        std::int64_t grace = 0;
        TokenPayload parsed;
        if (!exactInteger(value, "v", version) || version != 1 ||
            !exactString(value, "sub", parsed.subject) ||
            !exactString(value, "email", parsed.email) ||
            !exactString(value, "plan", parsed.plan) ||
            !exactInteger(value, "issued_at", parsed.issuedAt) ||
            !exactInteger(value, "paid_through", parsed.paidThrough) ||
            !exactInteger(value, "subscribed_since", parsed.subscribedSince) ||
            !exactInteger(value, "grace_days", grace) ||
            !exactString(value, "nonce", parsed.nonce) || grace < 7 || grace > 30 ||
            parsed.paidThrough <= 0 || parsed.issuedAt <= 0 || parsed.subscribedSince <= 0) {
            setError(error, "entitlement payload values are invalid");
            return false;
        }
        parsed.version = 1;
        parsed.graceDays = static_cast<int>(grace);
        payload = std::move(parsed);
        if (error) error->clear();
        return true;
    } catch (const std::exception&) {
        setError(error, "entitlement payload is not valid JSON");
        return false;
    }
}

bool verifyEmbeddedToken(std::string_view token, TokenPayload& payload, std::string* error) {
    return verifyToken(token, kEmbeddedPublicKey, payload, error);
}

bool verifyRefusal(std::string_view payloadJson,
                   const std::array<unsigned char, kPublicKeyBytes>& publicKey,
                   std::string_view expectedRequestNonce, std::int64_t nowWall,
                   std::string* reason, std::string* error) {
    try {
        json outer = json::parse(payloadJson);
        if (!outer.is_object() || outer.value("entitled", true) ||
            !outer.contains("proof") || !outer["proof"].is_string()) {
            setError(error, "refusal response shape is invalid");
            return false;
        }
        const std::string proof = outer["proof"].get<std::string>();
        outer.erase("proof");
        std::string_view encoded;
        std::string_view signature;
        if (!splitCompact(proof, encoded, signature)) {
            setError(error, "refusal proof is malformed");
            return false;
        }
        std::string signedJson;
        if (!verifyDetached(encoded, signature, publicKey, signedJson) || signedJson != outer.dump()) {
            setError(error, "refusal proof is invalid");
            return false;
        }
        const std::int64_t issuedAt = outer.value("issued_at", std::int64_t{0});
        (void)nowWall;  // signed telemetry only; a wrong client clock must not reject a refusal
        if (issuedAt <= 0) {
            setError(error, "refusal proof timestamp is invalid");
            return false;
        }
        if (outer.value("request_nonce", std::string()) != expectedRequestNonce ||
            outer.value("nonce", std::string()).empty()) {
            setError(error, "refusal proof does not match this refresh");
            return false;
        }
        if (reason) *reason = outer.value("reason", std::string("subscription inactive"));
        if (error) error->clear();
        return true;
    } catch (const std::exception&) {
        setError(error, "refusal response is not valid JSON");
        return false;
    }
}

int graceDaysFor(std::int64_t issuedAt, std::int64_t subscribedSince) {
    if (issuedAt <= subscribedSince) return 7;
    const std::int64_t months = (issuedAt - subscribedSince) / 2592000;
    return static_cast<int>(std::clamp<std::int64_t>(7 + months, 7, 30));
}

std::int64_t graceDeadline(const TokenPayload& payload,
                           std::int64_t lastSuccessfulCheckinWall) {
    const std::int64_t byCheckin = lastSuccessfulCheckinWall +
                                   static_cast<std::int64_t>(payload.graceDays) * 86400;
    const std::int64_t byPayment = payload.paidThrough + 3 * 86400;
    return std::min(byCheckin, byPayment);
}

OfflineDecision offlineAccess(const TokenPayload& payload,
                              const CheckinStamp& lastSuccessfulCheckin,
                              std::int64_t nowWall, double nowMonotonic) {
    OfflineDecision out;
    out.deadline = graceDeadline(payload, lastSuccessfulCheckin.wallSeconds);

    // A persisted steady-clock value is comparable only while it has not reset
    // (same boot). During that interval it is the authoritative elapsed clock.
    const bool sameMonotonicEpoch = lastSuccessfulCheckin.monotonicSeconds >= 0.0 &&
                                    nowMonotonic >= lastSuccessfulCheckin.monotonicSeconds;
    if (sameMonotonicEpoch) {
        const double elapsed = nowMonotonic - lastSuccessfulCheckin.monotonicSeconds;
        const double expectedWall = static_cast<double>(lastSuccessfulCheckin.wallSeconds) + elapsed;
        if (static_cast<double>(nowWall) < expectedWall - 3600.0) {
            out.access = OfflineAccess::ClockRollback;
            out.secondsRemaining = 0;
            return out;
        }
        const auto graceRemaining = static_cast<std::int64_t>(
            static_cast<double>(payload.graceDays * 86400) - elapsed);
        const auto paymentRemaining = payload.paidThrough + 3 * 86400 - nowWall;
        out.secondsRemaining = std::min(graceRemaining, paymentRemaining);
    } else {
        out.secondsRemaining = out.deadline - nowWall;
    }

    if (out.secondsRemaining <= 0) out.access = OfflineAccess::Expired;
    else if (out.secondsRemaining <= 3 * 86400) out.access = OfflineAccess::Expiring;
    else out.access = OfflineAccess::Allowed;
    return out;
}

std::array<unsigned char, 32> deviceKey(std::string_view stableDeviceMaterial) {
    std::array<unsigned char, crypto_hash_BYTES> digest{};
    crypto_hash(digest.data(),
                reinterpret_cast<const unsigned char*>(stableDeviceMaterial.data()),
                static_cast<unsigned long long>(stableDeviceMaterial.size()));
    std::array<unsigned char, 32> key{};
    std::copy_n(digest.begin(), key.size(), key.begin());
    return key;
}

std::vector<unsigned char> sealForDevice(std::string_view plaintext,
                                         const std::array<unsigned char, 32>& key) {
    std::array<unsigned char, crypto_secretbox_NONCEBYTES> nonce{};
    randombytes(nonce.data(), nonce.size());
    if (plaintext.size() > 1024 * 1024 ||
        plaintext.size() > std::numeric_limits<std::size_t>::max() - crypto_secretbox_ZEROBYTES)
        return {};
    std::vector<unsigned char> padded(crypto_secretbox_ZEROBYTES + plaintext.size(), 0);
    std::memcpy(padded.data() + crypto_secretbox_ZEROBYTES, plaintext.data(), plaintext.size());
    std::vector<unsigned char> boxed(padded.size());
    if (crypto_secretbox(boxed.data(), padded.data(), boxed.size(), nonce.data(), key.data()) != 0)
        return {};
    std::vector<unsigned char> out;
    constexpr unsigned char magic[] = {'C', 'W', 'E', '1'};
    out.insert(out.end(), std::begin(magic), std::end(magic));
    out.insert(out.end(), nonce.begin(), nonce.end());
    out.insert(out.end(), boxed.begin() + crypto_secretbox_BOXZEROBYTES, boxed.end());
    return out;
}

bool openForDevice(const std::vector<unsigned char>& sealed,
                   const std::array<unsigned char, 32>& key, std::string& plaintext) {
    constexpr std::size_t prefix = 4 + crypto_secretbox_NONCEBYTES;
    if (sealed.size() < prefix + crypto_secretbox_BOXZEROBYTES ||
        !std::equal(sealed.begin(), sealed.begin() + 4, "CWE1"))
        return false;
    const unsigned char* nonce = sealed.data() + 4;
    const std::size_t cipherLength = sealed.size() - prefix;
    std::vector<unsigned char> boxed(crypto_secretbox_BOXZEROBYTES + cipherLength, 0);
    std::memcpy(boxed.data() + crypto_secretbox_BOXZEROBYTES,
                sealed.data() + prefix, cipherLength);
    std::vector<unsigned char> opened(boxed.size());
    if (crypto_secretbox_open(opened.data(), boxed.data(), boxed.size(), nonce, key.data()) != 0)
        return false;
    plaintext.assign(reinterpret_cast<const char*>(opened.data() + crypto_secretbox_ZEROBYTES),
                     opened.size() - crypto_secretbox_ZEROBYTES);
    return true;
}

}  // namespace chudware::entitlement
