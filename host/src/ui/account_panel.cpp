#include "account_panel.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>
#include <sstream>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "../core/app_state.h"
#include "../ui_theme.h"
#include "layout.h"
#include "onboarding.h"
#include "web_capabilities.h"
#include "agent/http.h"
#include "api/chudware_version.h"
#include "security/containment.h"
#include "security/integrity.h"
#include "security/event_queue.h"
#include "util/device_identity.h"
#include "util/platform_paths.h"
#include "util/entitlement.h"
#include "util/secret_store.h"
extern "C" {
#include "util/tweetnacl.h"
}
extern "C" void randombytes(unsigned char*, unsigned long long);

#if defined(_WIN32)
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  include <windows.h>
using Socket = SOCKET;
static constexpr Socket kInvalidSocket = INVALID_SOCKET;
#else
#  include <arpa/inet.h>
#  include <netinet/in.h>
#  include <sys/socket.h>
#  include <sys/time.h>
#  include <unistd.h>
using Socket = int;
static constexpr Socket kInvalidSocket = -1;
#endif

namespace ui {
namespace {

using json = nlohmann::json;
using namespace std::chrono;
constexpr const char* kService = "com.chudware.desktop";
constexpr const char* kSecurityAccount = "security-policy-v1";
constexpr const char* kAccount = "entitlement";
constexpr const char* kSubjectAccount = "entitlement-subject-v1";
// Random per-install key that seals the stored entitlement. Before it existed the seal key was
// DERIVED from hostname + $USER + the appdata path, so any of those wobbling — an unset $USER
// under a .desktop/systemd launch, a renamed or DHCP-renamed host, a redirected XDG_DATA_HOME —
// made the sealed blob unopenable and signed the user out. The key is a secret in the same store
// as the token (OS keychain, else the 0600 fallback file), so its protection is the store's, not
// the guessability of a hostname.
constexpr const char* kSealKeyAccount = "entitlement-seal-key-v1";
constexpr double kRefreshIntervalSeconds = 12.0 * 60.0 * 60.0;
// After a failed check the next attempt comes much sooner — 5 min, doubling to the normal
// interval — so a machine that comes back online recovers on its own within minutes.
constexpr double kRetryBaseSeconds = 5.0 * 60.0;
constexpr const char* kUnconfirmedStatus =
    "Your subscription couldn't be confirmed — check your connection or sign in again";
constexpr const char* kReauthorizeStatus = "Sign in again to keep editing on this device";
using account_detail::PendingResult;
using account_detail::Operation;

struct AccountWorker {
    std::thread thread;
    std::atomic<bool> done{false};
};

struct Controller {
    account_detail::ResultMailbox results;
    // Only the UI thread owns jobs and session state. Workers only publish immutable
    // responses to results; no worker writes credentials, queue state or AppState.
    std::vector<std::unique_ptr<AccountWorker>> workers;
    std::atomic<bool> stopping{false};
    chudware::entitlement::TokenPayload payload;
    chudware::entitlement::CheckinStamp stamp;
    double lastAttemptMonotonic = -1.0;
    bool initialized = false;
    std::string securityPolicyId;
    bool authoritativeRefused = false;
    int failedAttempts = 0;  // consecutive unreachable checks; drives the retry backoff

    ~Controller() {
        stopping.store(true);
        for (const auto& worker : workers)
            if (worker->thread.joinable()) worker->thread.join();
    }
};

Controller& controller() {
    static Controller value;
    return value;
}

template <typename Work>
void launchAccountWorker(Controller& c, Work work) {
    auto worker = std::make_unique<AccountWorker>();
    AccountWorker* job = worker.get();
    job->thread = std::thread([job, work = std::move(work)]() mutable {
        work();
        job->done.store(true, std::memory_order_release);
    });
    c.workers.push_back(std::move(worker));
}

void reapAccountWorkers(Controller& c) {
    for (auto it = c.workers.begin(); it != c.workers.end();) {
        if (!(*it)->done.load(std::memory_order_acquire)) {
            ++it;
            continue;
        }
        (*it)->thread.join();
        it = c.workers.erase(it);
    }
}

double monotonicNow() {
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

std::string platformName();
std::int64_t wallNow() {
    return duration_cast<seconds>(system_clock::now().time_since_epoch()).count();
}

std::string bootIdentifier() {
#if defined(_WIN32)
    // The boot wall instant remains stable for the boot and changes across reboots.
    const auto bootWall = wallNow() - static_cast<std::int64_t>(GetTickCount64() / 1000ULL);
    return "windows:" + std::to_string(bootWall);
#elif defined(__linux__)
    std::ifstream input("/proc/sys/kernel/random/boot_id");
    std::string id;
    std::getline(input, id);
    return id.empty() ? std::string{} : "linux:" + id;
#else
    // No reliable platform boot identity: leave empty and force wall-clock mode.
    return {};
#endif
}

void closeSocket(Socket socket) {
#if defined(_WIN32)
    closesocket(socket);
#else
    ::close(socket);
#endif
}

std::string deviceName() {
    std::array<char, 256> name{};
#if defined(_WIN32)
    DWORD length = static_cast<DWORD>(name.size());
    if (GetComputerNameA(name.data(), &length)) return std::string(name.data(), length);
#else
    if (::gethostname(name.data(), name.size() - 1) == 0) return name.data();
#endif
    return "this computer";
}

// LEGACY seal material — hostname + user + appdata path. Kept ONLY so an entitlement sealed by an
// older build can be opened once and re-sealed under the persisted key (see sealKeys()). Never used
// to seal anything new.
std::string legacyDeviceMaterial(bool withUser) {
    std::string material = deviceName();
    material.push_back('\n');
    if (withUser) {
#if defined(_WIN32)
        if (const char* user = std::getenv("USERNAME")) material += user;
#else
        if (const char* user = std::getenv("USER")) material += user;
#endif
    }
    material.push_back('\n');
    material += appdataAppDir();
    return material;
}

// The persisted per-install seal key, created on first use. Returns false only when the store
// itself refuses both the read and the write — in that case the caller must NOT treat a failed
// unseal as a damaged token.
bool sealKey(std::array<unsigned char, 32>& key, bool& created) {
    created = false;
    std::vector<unsigned char> stored;
    if (chudware::secret_store::load(kService, kSealKeyAccount, stored, appdataAppDir()) &&
        stored.size() == key.size()) {
        std::copy(stored.begin(), stored.end(), key.begin());
        return true;
    }
    randombytes(key.data(), key.size());
    created = true;
    return chudware::secret_store::store(
        kService, kSealKeyAccount,
        std::vector<unsigned char>(key.begin(), key.end()), appdataAppDir());
}

std::string urlEncode(std::string_view value) {
    static constexpr char hex[] = "0123456789ABCDEF";
    std::string out;
    for (unsigned char c : value) {
        if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out.push_back(c);
        else {
            out.push_back('%');
            out.push_back(hex[c >> 4]);
            out.push_back(hex[c & 15]);
        }
    }
    return out;
}

int hexValue(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

std::string urlDecode(std::string_view value) {
    std::string out;
    for (std::size_t i = 0; i < value.size(); ++i) {
        if (value[i] == '%' && i + 2 < value.size()) {
            const int hi = hexValue(value[i + 1]);
            const int lo = hexValue(value[i + 2]);
            if (hi >= 0 && lo >= 0) {
                out.push_back(static_cast<char>((hi << 4) | lo));
                i += 2;
                continue;
            }
        }
        out.push_back(value[i] == '+' ? ' ' : value[i]);
    }
    return out;
}

std::string queryValue(std::string_view target, std::string_view name) {
    const std::size_t question = target.find('?');
    if (question == std::string_view::npos) return {};
    std::string_view query = target.substr(question + 1);
    while (!query.empty()) {
        const std::size_t amp = query.find('&');
        const std::string_view pair = query.substr(0, amp);
        const std::size_t equals = pair.find('=');
        if (equals != std::string_view::npos && pair.substr(0, equals) == name)
            return urlDecode(pair.substr(equals + 1));
        if (amp == std::string_view::npos) break;
        query.remove_prefix(amp + 1);
    }
    return {};
}

std::vector<unsigned char> decodeUrl64(std::string_view value) {
    auto cv = [](unsigned char c) -> int {
        if (c >= 'A' && c <= 'Z') return c - 'A';
        if (c >= 'a' && c <= 'z') return c - 'a' + 26;
        if (c >= '0' && c <= '9') return c - '0' + 52;
        if (c == '-') return 62;
        if (c == '_') return 63;
        return -1;
    };
    if (value.empty() || value.find('=') != std::string_view::npos || value.size() % 4 == 1)
        return {};
    std::vector<unsigned char> out;
    unsigned int acc = 0;
    int bits = 0;
    for (unsigned char c : value) {
        const int v = cv(c);
        if (v < 0) return {};
        acc = (acc << 6) | static_cast<unsigned int>(v);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out.push_back(static_cast<unsigned char>((acc >> bits) & 255));
        }
    }
    return out;
}

// Launch-site verification is intentionally independent of the mutation and AI
// checks. It calls TweetNaCl directly before the shared strict payload parser.
bool launchSignatureValid(std::string_view token) {
    const std::size_t dot = token.find('.');
    if (dot == std::string_view::npos || token.find('.', dot + 1) != std::string_view::npos)
        return false;
    const std::string_view encoded = token.substr(0, dot);
    const auto signature = decodeUrl64(token.substr(dot + 1));
    if (signature.size() != crypto_sign_BYTES) return false;
    std::vector<unsigned char> signedMessage(signature.size() + encoded.size());
    std::copy(signature.begin(), signature.end(), signedMessage.begin());
    std::copy(encoded.begin(), encoded.end(), signedMessage.begin() + signature.size());
    std::vector<unsigned char> opened(signedMessage.size());
    unsigned long long openedLength = 0;
    return crypto_sign_open(opened.data(), &openedLength, signedMessage.data(),
                            static_cast<unsigned long long>(signedMessage.size()),
                            chudware::entitlement::kEmbeddedPublicKey.data()) == 0 &&
           openedLength == encoded.size();
}

bool persist(const std::string& token, const chudware::entitlement::CheckinStamp& stamp) {
    json stored = {
        {"token", token},
        {"last_successful_checkin_wall", stamp.wallSeconds},
        {"last_successful_checkin_monotonic", stamp.monotonicSeconds},
        {"last_successful_checkin_boot_id", stamp.bootId},
    };
    std::array<unsigned char, 32> key{};
    bool created = false;
    if (!sealKey(key, created)) return false;
    const auto sealed = chudware::entitlement::sealForDevice(stored.dump(), key);
    return !sealed.empty() && chudware::secret_store::store(
        kService, kAccount, sealed, appdataAppDir());
}

bool persistSubject(std::string_view subject) {
    if (subject.empty()) return false;
    return chudware::secret_store::store(
        kService, kSubjectAccount,
        std::vector<unsigned char>(subject.begin(), subject.end()), appdataAppDir());
}

// Load outcome. `Missing` and `Unreadable` are DIFFERENT: nothing was ever stored versus a stored
// blob this launch could not open. Only the first justifies "sign in"; the second must leave the
// blob alone, because the fault may be the environment (an unavailable keychain, a store the
// process cannot read yet) and destroying the session would be unrecoverable.
enum class StoredLoad { Ok, Missing, Unreadable };

StoredLoad loadStored(std::string& token, chudware::entitlement::CheckinStamp& stamp) {
    std::vector<unsigned char> sealed;
    if (!chudware::secret_store::load(kService, kAccount, sealed, appdataAppDir()))
        return StoredLoad::Missing;

    // Current key first, then the two legacy derivations (with and without $USER — an unset $USER
    // under a desktop-file or systemd launch produced the second spelling). A legacy hit is
    // re-sealed under the persisted key so the migration happens once, silently.
    std::string plaintext;
    bool viaLegacy = false;
    std::array<unsigned char, 32> key{};
    bool created = false;
    const bool haveKey = sealKey(key, created);
    bool opened = haveKey && !created &&
                  chudware::entitlement::openForDevice(sealed, key, plaintext);
    if (!opened) {
        for (const bool withUser : {true, false}) {
            if (chudware::entitlement::openForDevice(
                    sealed, chudware::entitlement::deviceKey(legacyDeviceMaterial(withUser)),
                    plaintext)) {
                opened = true;
                viaLegacy = true;
                break;
            }
        }
    }
    if (!opened) return StoredLoad::Unreadable;

    try {
        const json stored = json::parse(plaintext);
        token = stored.at("token").get<std::string>();
        stamp.wallSeconds = stored.at("last_successful_checkin_wall").get<std::int64_t>();
        stamp.monotonicSeconds = stored.at("last_successful_checkin_monotonic").get<double>();
        stamp.bootId = stored.at("last_successful_checkin_boot_id").get<std::string>();
        if (token.empty() || stamp.wallSeconds <= 0) return StoredLoad::Unreadable;
    } catch (const std::exception&) {
        return StoredLoad::Unreadable;
    }
    if (viaLegacy) persist(token, stamp);   // best effort: a failed re-seal still signs in
    return StoredLoad::Ok;
}

bool persistSecurityPolicy(std::string_view subject, const json& policy) {
    chudware::entitlement::SecurityPolicy verified;
    std::string error;
    if (!chudware::entitlement::verifyEmbeddedSecurityPolicy(
            policy.dump(), subject, verified, &error)) return false;
    const std::string value = json{{"subject",subject},{"policy",policy}}.dump();
    return chudware::secret_store::store(kService,kSecurityAccount,
        std::vector<unsigned char>(value.begin(),value.end()),appdataAppDir());
}

bool loadSecurityPolicy(std::string& subject, json& policy,
                        chudware::entitlement::SecurityPolicy& verified) {
    std::vector<unsigned char> bytes;
    if(!chudware::secret_store::load(kService,kSecurityAccount,bytes,appdataAppDir()))return false;
    try {
        const json value=json::parse(bytes.begin(),bytes.end());
        subject=value.at("subject").get<std::string>(); policy=value.at("policy");
        std::string error;
        return !subject.empty() && chudware::entitlement::verifyEmbeddedSecurityPolicy(
            policy.dump(), subject, verified, &error);
    } catch(...){return false;}
}

std::string securityEventsUrl() {
    if(const char* value=std::getenv("CHUDWARE_SECURITY_EVENTS_URL");value&&*value)return value;
    return "https://app.chudware.com/api/security/events";
}

struct SecurityBatchResult {
    bool accepted = false;
    bool reauthorize = false;  // signed gateway verdict: this device must sign in again
    json customerReceipt = nullptr;
    std::string enforcementState;
    std::string batchId;
    std::uint64_t batchSequence = 0;
};

SecurityBatchResult sendSecurityBatch(std::string_view subject,
                                      const chudware::entitlement::SecurityPolicy& policy,
                                      const chudware::security::SecurityBatch& batch,
                                      const std::string& compressed,
                                      std::uint64_t refreshSequence,
                                      std::string_view requestNonce) {
    SecurityBatchResult result;
    std::string error;
    const std::string endpoint = policy.id == "customer_endpoint"
        ? policy.customerEndpoint : securityEventsUrl();
    if (policy.id == "customer_endpoint" && endpoint.rfind("https://", 0) != 0) return result;
    const auto response=chudware::agent::http_post(endpoint,
        {{"Content-Type","application/json"},{"Content-Encoding","gzip"}},compressed,8000);
    if (policy.id == "customer_endpoint") {
        if (response.status != 200) return result;
        try {
            const json acceptance = json::parse(response.body);
            if (acceptance != json{{"accepted", true}}) return result;
            json receipt = {
                {"subject", subject},
                {"sequence", refreshSequence},
                {"request_nonce", requestNonce},
                {"verdict", "allow"},
                {"reason", "heartbeat"},
                {"issued_at", wallNow()},
            };
            // The customer service authenticates acceptance through HTTPS; the
            // device then attests that exact refresh-bound acceptance to central.
            if (!chudware::device_identity::signCustomerReceipt(
                    receipt, appdataAppDir(), &error)) return result;
            result.customerReceipt = std::move(receipt);
            result.accepted = true;
            return result;
        } catch (...) { return result; }
    }
    std::string enforcement, status;
    if(response.status!=200||!chudware::security::verifySecurityAcknowledgement(
            response.body,batch.batchId,subject,policy.id,batch.sequence,&enforcement,&status))return result;
    if(status=="reauthorize"){result.reauthorize=true;return result;}
    const auto decision=chudware::security::verifyEmbeddedEnforcementState(enforcement,subject,wallNow());
    if(decision.status==chudware::security::EnforcementStatus::Malformed||
       decision.status==chudware::security::EnforcementStatus::InvalidSignature||
       decision.status==chudware::security::EnforcementStatus::SubjectMismatch)return result;
    // Applying the acknowledgement belongs to the UI's current-generation commit,
    // never the network worker. A delayed acknowledgement cannot drain a new queue.
    result.enforcementState = std::move(enforcement);
    result.batchId = batch.batchId;
    result.batchSequence = batch.sequence;
    result.accepted = true;
    return result;
}

void fillState(AppState& state, const chudware::entitlement::TokenPayload& payload) {
    state.entitlementSignedIn = true;
    state.entitlementPlan = payload.plan;
    state.entitlementEmail = payload.email;
}

void evaluateOffline(AppState& state) {
    Controller& c = controller();
    const auto decision = chudware::entitlement::offlineAccess(
        c.payload, c.stamp, wallNow(), monotonicNow(), bootIdentifier());
    state.entitlementReadOnly =
        decision.access == chudware::entitlement::OfflineAccess::Expired ||
        decision.access == chudware::entitlement::OfflineAccess::ClockRollback;
    // Customer-facing wording stays generic on purpose: no countdowns, windows or clock
    // checks — naming them only tells someone probing the binary what to look for.
    if (state.entitlementReadOnly)
        state.entitlementStatus = kUnconfirmedStatus;
    else
        state.entitlementStatus.clear();
}

std::string refreshUrl() {
    if (const char* overrideUrl = std::getenv("CHUDWARE_ENTITLEMENT_REFRESH_URL");
        overrideUrl && *overrideUrl)
        return overrideUrl;
    return "https://app.chudware.com/api/entitlement/refresh";
}

bool setPending(std::uint64_t generation, PendingResult result) {
    result.generation = generation;  // the immutable launch generation, not current state
    return controller().results.complete(std::move(result));
}

std::string randomState();

std::string platformName() {
#if defined(_WIN32)
    return "windows";
#elif defined(__APPLE__)
    return "macos";
#else
    return "linux";
#endif
}

std::string integrityDigest() {
    const auto verified = chudware::security::verifyInstalledPayload(
        chudware::platform::executablePath(), chudware::security::VerifyMode::Startup);
    if (!verified.ok) return "invalid:" + std::string(chudware::security::integrityReasonName(verified.reason));
    // Startup verification is authoritative for the installed signed payload.
    // The digest is a stable signed-manifest identifier rather than a claim
    // that the device itself is trustworthy.
    const auto manifest = verified.root / "payload-manifest.v1.json";
    std::ifstream input(manifest, std::ios::binary);
    if (!input) return chudware::security::integrityReasonName(verified.reason);
    std::string bytes((std::istreambuf_iterator<char>(input)), {});
    std::array<unsigned char, crypto_hash_BYTES> hash{};
    crypto_hash(hash.data(), reinterpret_cast<const unsigned char*>(bytes.data()), bytes.size());
    static constexpr char hex[] = "0123456789abcdef";
    std::string out;
    out.reserve(hash.size() * 2);
    for (unsigned char byte : hash) { out.push_back(hex[byte >> 4]); out.push_back(hex[byte & 15]); }
    return out;
}

void startRefresh(const std::string& token) {
    Controller& c = controller();
    const std::uint64_t generation = c.results.begin(Operation::Refresh);
    if (generation == 0) return;
    c.lastAttemptMonotonic = monotonicNow();
    const auto identity = chudware::device_identity::load(appdataAppDir());
    if (identity.status != chudware::device_identity::KeyStatus::Ready) {
        setPending(generation, {PendingResult::Kind::Invalid, {},
                   "Device identity is missing or damaged; sign in again to reauthorize this device"});
        return;
    }
    // Session-bound writes are UI-owned and happen before the worker is launched.
    // In particular prepareBatch changes queue counters: doing it on an obsolete
    // worker would reset the next account's queue. No mailbox lock is held for I/O.
    std::string securitySubject;
    json signedPolicy;
    chudware::entitlement::SecurityPolicy securityPolicy;
    const bool havePolicy = loadSecurityPolicy(securitySubject, signedPolicy, securityPolicy);
    const std::string challenge = randomState();
    const std::uint64_t sequence = chudware::device_identity::nextRefreshSequence(appdataAppDir());
    chudware::security::EventQueue queue(appdataAppDir());
    const std::uint64_t lastAcknowledged = queue.acceptedThrough(securitySubject);
    chudware::security::SecurityBatch securityBatch;
    std::string compressed;
    if (havePolicy) {
        std::string error;
        securityBatch = queue.prepareBatch(
            securitySubject, securityPolicy.id, CHUDWARE_VERSION, platformName(), wallNow(), &error);
        if (securityBatch && !chudware::security::gzipSecurityPayload(securityBatch.body, compressed))
            securityBatch = {};
    }
    // HTTPS and response verification remain on the worker. Responses contain
    // proposed side effects only; the current UI generation commits them in tick.
    launchAccountWorker(c, [token, generation, securitySubject = std::move(securitySubject),
                           securityPolicy = std::move(securityPolicy), havePolicy,
                           challenge, sequence, lastAcknowledged,
                           securityBatch = std::move(securityBatch),
                           compressed = std::move(compressed)]() {
        SecurityBatchResult batch;
        auto finish = [&](PendingResult result) {
            result.acknowledgedEnforcement = std::move(batch.enforcementState);
            result.batchId = std::move(batch.batchId);
            result.batchSequence = batch.batchSequence;
            setPending(generation, std::move(result));
        };
        if (!controller().results.current(generation)) return;
        json customerReceipt = nullptr;
        if (havePolicy && securityBatch) {
            batch = sendSecurityBatch(
                securitySubject, securityPolicy, securityBatch, compressed, sequence, challenge);
            if (!controller().results.current(generation)) return;
            if (batch.reauthorize) {
                finish({PendingResult::Kind::Refused, {}, kReauthorizeStatus});
                return;
            }
        }
        if (havePolicy && securityPolicy.id == "customer_endpoint") {
            if (!batch.accepted || batch.customerReceipt.is_null()) {
                finish({PendingResult::Kind::Unreachable, {}, {}});
                return;
            }
            customerReceipt = std::move(batch.customerReceipt);
        }
        json envelope = {
            {"token", token},
            {"request_nonce", challenge},
            {"sequence", sequence},
            {"app_version", CHUDWARE_VERSION},
            {"platform", platformName()},
            {"integrity_digest", integrityDigest()},
            // The gateway has accepted this sequence even though local acknowledgement
            // persistence is deferred until the result's current-generation commit.
            {"last_acked_event_sequence",
             batch.batchSequence != 0 ? batch.batchSequence : lastAcknowledged},
            {"customer_receipt", std::move(customerReceipt)},
        };
        std::string signingError;
        if (sequence == 0 ||
            !chudware::device_identity::signRefreshEnvelope(envelope, appdataAppDir(), &signingError)) {
            finish({PendingResult::Kind::Invalid, {},
                    "Device identity could not sign the refresh; sign in again to reauthorize this device"});
            return;
        }
        if (!controller().results.current(generation)) return;
        const auto response = chudware::agent::http_post(
            refreshUrl(), {{"Content-Type", "application/json"},
                           {"X-Chudware-Refresh-Nonce", challenge}}, envelope.dump(), 8000);
        if (!controller().results.current(generation)) return;
        if (response.status == 200) {
            try {
                const json body = json::parse(response.body);
                if (body.value("entitled", false) && body.contains("token") && body["token"].is_string()) {
                    chudware::entitlement::TokenPayload fresh;
                    std::string error;
                    const std::string freshToken = body["token"].get<std::string>();
                    if (chudware::entitlement::verifyEmbeddedToken(freshToken, fresh, &error)) {
                        chudware::entitlement::SecurityPolicy verifiedPolicy;
                        if (!body.contains("security_policy") || !body["security_policy"].is_object() ||
                            !chudware::entitlement::verifyEmbeddedSecurityPolicy(
                                body["security_policy"].dump(), fresh.subject, verifiedPolicy, &error)) {
                            finish({PendingResult::Kind::Invalid, {}, "Gateway security policy failed verification"});
                            return;
                        }
                        if (!body.contains("enforcement_state") || !body["enforcement_state"].is_object()) {
                            finish({PendingResult::Kind::Invalid, {}, "Gateway omitted signed enforcement state"});
                            return;
                        }
                        const std::string enforcement = body["enforcement_state"].dump();
                        const auto decision = chudware::security::verifyEmbeddedEnforcementState(
                            enforcement, fresh.subject, wallNow());
                        if (decision.status == chudware::security::EnforcementStatus::InvalidSignature ||
                            decision.status == chudware::security::EnforcementStatus::SubjectMismatch ||
                            decision.status == chudware::security::EnforcementStatus::Malformed) {
                            finish({PendingResult::Kind::Invalid, {}, "Gateway enforcement state failed verification"});
                            return;
                        }
                        finish({PendingResult::Kind::Fresh, freshToken, {},
                                body["security_policy"].dump(), enforcement});
                        return;
                    }
                    finish({PendingResult::Kind::Invalid, {}, "Gateway returned an invalid entitlement"});
                    return;
                }
            } catch (const std::exception&) {
                // A signed refusal is verified against the raw body below.
            }
            std::string reason;
            std::string error;
            if (chudware::entitlement::verifyRefusal(
                    response.body, chudware::entitlement::kEmbeddedPublicKey,
                    challenge, wallNow(), &reason, &error)) {
                finish({PendingResult::Kind::Refused, {}, reason});
                return;
            }
        }
        finish({PendingResult::Kind::Unreachable, {}, {}});
    });
}

void setSocketReceiveTimeout(Socket socket) {
#if defined(_WIN32)
    DWORD timeout = 1000;
    setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&timeout), sizeof(timeout));
#else
    timeval timeout{1, 0};
    setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
#endif
}

Socket makeListener(unsigned short& port) {
#if defined(_WIN32)
    static std::once_flag winsock;
    std::call_once(winsock, [] { WSADATA data{}; WSAStartup(MAKEWORD(2, 2), &data); });
#endif
    Socket socket = ::socket(AF_INET, SOCK_STREAM, 0);
    if (socket == kInvalidSocket) return kInvalidSocket;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    if (::bind(socket, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0 ||
        ::listen(socket, 1) != 0) {
        closeSocket(socket);
        return kInvalidSocket;
    }
    socklen_t length = sizeof(address);
    if (::getsockname(socket, reinterpret_cast<sockaddr*>(&address), &length) != 0) {
        closeSocket(socket);
        return kInvalidSocket;
    }
    port = ntohs(address.sin_port);
    setSocketReceiveTimeout(socket);
    return socket;
}

std::string randomState() {
    std::array<unsigned char, 32> bytes{};
    randombytes(bytes.data(), bytes.size());
    static constexpr char chars[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    std::string out;
    unsigned int acc = 0;
    int bits = 0;
    for (unsigned char byte : bytes) {
        acc = (acc << 8) | byte;
        bits += 8;
        while (bits >= 6) {
            bits -= 6;
            out.push_back(chars[(acc >> bits) & 63]);
        }
    }
    if (bits) out.push_back(chars[(acc << (6 - bits)) & 63]);
    return out;
}

// The loopback callback page is the ONLY Chudware surface that renders in the user's browser
// during sign-in, so it is branded and self-contained: no external CSS, fonts or images (the
// listener answers exactly one request and then closes, and the tab is offline-loopback, so any
// remote reference would just render as a broken box). `Cache-Control: no-store` and
// `Referrer-Policy: no-referrer` keep the token-bearing URL out of caches and out of any
// onward request.
std::string browserPageHtml(bool ok) {
    const char* title = ok ? "You're signed in" : "Authorization failed";
    const char* detail = ok
        ? "This device is now linked to your Chudware account. Close this tab and return to "
          "the app &mdash; editing is already unlocked there."
        : "Chudware could not verify this authorization. Close this tab, return to the app and "
          "start sign-in again.";
    // Hardcoded on purpose: this page is served to an external browser over loopback,
    // so it cannot read ui::palette — the native theme, and any accent the user picked
    // in Settings, are unreachable from here. #F28216 is the default brand primary; if
    // that default ever moves, this literal has to move with it by hand. The failure
    // red stays as-is: a failure is not an accent and must not follow the brand.
    const char* accent = ok ? "#F28216" : "#e0605e";
    return std::string(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Chudware &middot; ") + title + "</title><style>"
        ":root{color-scheme:dark}"
        "*{box-sizing:border-box}"
        "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
        "background:radial-gradient(1200px 600px at 50% -10%,#1b2733 0%,#0e141b 55%,#0a0f14 100%);"
        "color:#e8edf2;font:16px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Inter,"
        "Roboto,'Helvetica Neue',Arial,sans-serif;padding:32px}"
        ".card{width:100%;max-width:460px;padding:36px 34px 32px;border-radius:18px;"
        "background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.10);"
        "box-shadow:0 24px 60px rgba(0,0,0,.45);text-align:center}"
        ".mark{width:56px;height:56px;margin:0 auto 18px;display:block}"
        ".brand{font-size:12px;letter-spacing:.18em;text-transform:uppercase;"
        "color:rgba(232,237,242,.55);margin:0 0 22px}"
        "h1{margin:0 0 10px;font-size:24px;font-weight:600;letter-spacing:-.01em}"
        "p{margin:0;color:rgba(232,237,242,.72)}"
        ".pill{display:inline-flex;align-items:center;gap:8px;margin-top:24px;padding:7px 14px;"
        "border-radius:999px;font-size:13px;background:rgba(255,255,255,.05);"
        "border:1px solid rgba(255,255,255,.09);color:rgba(232,237,242,.66)}"
        ".dot{width:8px;height:8px;border-radius:50%;background:" + accent + "}"
        "</style></head><body><main class=\"card\">"
        "<svg class=\"mark\" viewBox=\"0 0 64 64\" fill=\"none\" aria-hidden=\"true\">"
        "<rect x=\"6\" y=\"6\" width=\"38\" height=\"38\" rx=\"3\" stroke=\"" + accent +
        "\" stroke-width=\"2.4\"/>"
        "<rect x=\"20\" y=\"20\" width=\"38\" height=\"38\" rx=\"3\" stroke=\"" + accent +
        "\" stroke-width=\"2.4\" stroke-opacity=\".65\"/>"
        "<path d=\"M6 6l14 14M44 6l14 14M6 44l14 14M44 44l14 14\" stroke=\"" + accent +
        "\" stroke-width=\"2.4\" stroke-opacity=\".45\"/></svg>"
        "<p class=\"brand\">Chudware</p><h1>" + title + "</h1><p>" + detail + "</p>"
        "<span class=\"pill\"><span class=\"dot\"></span>" +
        (ok ? "Device authorized" : "Not authorized") +
        "</span></main></body></html>";
}

void sendBrowserResponse(Socket client, bool ok) {
    const std::string body = browserPageHtml(ok);
    const std::string response = std::string("HTTP/1.1 ") + (ok ? "200 OK" : "400 Bad Request") +
        "\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store"
        "\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\nContent-Length: " +
        std::to_string(body.size()) + "\r\n\r\n" + body;
    (void)::send(client, response.data(), static_cast<int>(response.size()), 0);
}

}  // namespace

void initializeAccountPanel(AppState& state) {
    Controller& c = controller();
    if (c.initialized) return;
    c.initialized = true;
    setOnboardingAccountLoginSeam([](AppState& st, OnboardingAccountAction action,
                                     OnboardingAccountSummary& summary) {
        if (action == OnboardingAccountAction::BeginLogin) beginDesktopAccountLogin(st);
        else if (action == OnboardingAccountAction::Refresh && st.entitlementSignedIn)
            startRefresh(st.entitlementToken);
        summary.signedIn = st.entitlementSignedIn;
        summary.plan = st.entitlementPlan;
        summary.pooledKeysAllowed = summary.plan == "trial" || summary.plan == "studio" ||
                                    summary.plan == "enterprise";
        summary.creditBalance = 0;
        summary.detail = st.entitlementStatus;
        return action != OnboardingAccountAction::UseCredits || summary.pooledKeysAllowed;
    });

    // A stored session is only DISCARDED on explicit sign-out (signOutDesktopAccount). Startup
    // never erases it: a launch that cannot open or verify the blob reports read-only and leaves
    // the bytes in place, so the next launch — after the keychain is available again, or once the
    // legacy-material migration in loadStored() succeeds — signs the user back in instead of
    // demanding credentials. Erasing here is what made sign-ins evaporate.
    std::string token;
    const StoredLoad stored = loadStored(token, c.stamp);
    if (stored != StoredLoad::Ok || !launchSignatureValid(token)) {
        state.entitlementReadOnly = true;
        state.entitlementSignedIn = false;
        state.entitlementStatus =
            stored == StoredLoad::Missing
                ? "Sign in to activate desktop editing"
                : "Stored sign-in could not be read on this launch — retrying next start; sign in to activate editing now";
        return;
    }
    std::string error;
    if (!chudware::entitlement::verifyEmbeddedToken(token, c.payload, &error)) {
        state.entitlementReadOnly = true;
        state.entitlementSignedIn = false;
        state.entitlementStatus = "Stored entitlement did not verify; sign in again";
        return;
    }
    state.entitlementToken = token;
    fillState(state, c.payload);
    evaluateOffline(state);
    startRefresh(token);
}

bool beginDesktopAccountLogin(AppState& state) {
    Controller& c = controller();
    if (c.results.running() == Operation::Login) return false;
    unsigned short port = 0;
    Socket listener = makeListener(port);
    if (listener == kInvalidSocket) {
        state.entitlementStatus = "Could not start the secure loopback sign-in listener";
        return false;
    }
    const auto identity = chudware::device_identity::prepareAuthorizationKey(appdataAppDir());
    if (identity.status != chudware::device_identity::KeyStatus::Ready) {
        closeSocket(listener);
        state.entitlementStatus = "Could not create or securely store the pending device identity";
        return false;
    }
    const std::string stateValue = randomState();
    const std::string device = deviceName();
    const char* baseEnv = std::getenv("CHUDWARE_ENTITLEMENT_AUTHORIZE_URL");
    const std::string base = baseEnv && *baseEnv
        ? baseEnv : "https://app.chudware.com/desktop/authorize";
    const std::string url = base + "?port=" + std::to_string(port) +
        "&state=" + urlEncode(stateValue) + "&device=" + urlEncode(device) +
        "&device_public_key=" + urlEncode(identity.publicKey);
    if (!openExternalHttpUrl(url)) {
        closeSocket(listener);
        chudware::device_identity::discardAuthorizationKey(appdataAppDir());
        state.entitlementStatus = "Could not open the system browser";
        return false;
    }
    const std::uint64_t generation = c.results.begin(Operation::Login);
    state.entitlementStatus = "Waiting for browser authorization";
    launchAccountWorker(c, [listener, stateValue, generation]() {
        const double deadline = monotonicNow() + 300.0;
        while (!controller().stopping.load() && controller().results.current(generation) &&
               monotonicNow() < deadline) {
            sockaddr_in peer{};
            socklen_t peerLength = sizeof(peer);
            Socket client = ::accept(listener, reinterpret_cast<sockaddr*>(&peer), &peerLength);
            if (client == kInvalidSocket) continue;
            // Accepted sockets do not inherit SO_RCVTIMEO on every platform. Keep
            // cancelled generations reapable even if a callback connects then stalls.
            setSocketReceiveTimeout(client);
            std::array<char, 16384> buffer{};
            const int count = static_cast<int>(::recv(client, buffer.data(), buffer.size() - 1, 0));
            bool ok = false;
            if (count > 0) {
                std::string_view request(buffer.data(), static_cast<std::size_t>(count));
                const std::size_t firstSpace = request.find(' ');
                const std::size_t secondSpace = firstSpace == std::string_view::npos
                    ? std::string_view::npos : request.find(' ', firstSpace + 1);
                if (request.substr(0, firstSpace) == "GET" && secondSpace != std::string_view::npos) {
                    const std::string_view target = request.substr(firstSpace + 1, secondSpace - firstSpace - 1);
                    const std::string token = queryValue(target, "token");
                    const std::string returnedState = queryValue(target, "state");
                    chudware::entitlement::TokenPayload payload;
                    std::string error;
                    ok = target.rfind("/callback?", 0) == 0 && returnedState == stateValue &&
                         chudware::entitlement::verifyEmbeddedToken(token, payload, &error);
                    if (ok) {
                        try {
                            const json policy = json::parse(queryValue(target, "security_policy"));
                            const json enforcement = json::parse(queryValue(target, "enforcement_state"));
                            const auto decision = chudware::security::verifyEmbeddedEnforcementState(
                                enforcement.dump(), payload.subject, wallNow());
                            chudware::entitlement::SecurityPolicy verifiedPolicy;
                            ok = policy.is_object() && policy.value("subject", "") == payload.subject &&
                                 policy.value("request_nonce", "") == stateValue && decision.active() &&
                                 chudware::entitlement::verifyEmbeddedSecurityPolicy(
                                     policy.dump(), payload.subject, verifiedPolicy, &error);
                            if (ok)
                                ok = setPending(generation, {PendingResult::Kind::Fresh, token, {},
                                                            policy.dump(), enforcement.dump()});
                        } catch (...) { ok = false; }
                    }
                }
            }
            sendBrowserResponse(client, ok);
            closeSocket(client);
            if (ok) { closeSocket(listener); return; }
        }
        closeSocket(listener);
        setPending(generation, {PendingResult::Kind::Unreachable, {}, "Browser authorization timed out"});
    });
    return true;
}

bool desktopAccountRequestsIdle() {
    const Controller& c = controller();
    return c.workers.empty() && c.results.running() == Operation::None;
}

void tickAccountPanel(AppState& state) {
    Controller& c = controller();
    reapAccountWorkers(c);  // only done workers; never wait for HTTPS or loopback on the UI
    bool resultError = false;
    c.results.consume([&](PendingResult result) {
        // consume is UI-owned, serialized with launch and explicit sign-out. All
        // effects below belong to the captured generation, including queue acceptance.
        if (result.operation == Operation::Login && result.kind != PendingResult::Kind::Fresh)
            chudware::device_identity::discardAuthorizationKey(appdataAppDir());
        if (!result.acknowledgedEnforcement.empty()) {
            std::string error;
            if (!chudware::security::persistEnforcementState(result.acknowledgedEnforcement, appdataAppDir()) ||
                !chudware::security::EventQueue(appdataAppDir()).accept(
                    result.batchId, result.batchSequence, &error)) {
                state.entitlementReadOnly = true;
                state.entitlementStatus = "Account update could not be stored securely";
                resultError = true;
                return;
            }
        }
        if (result.kind == PendingResult::Kind::Fresh) {
            chudware::entitlement::TokenPayload fresh;
            std::string error;
            if (!chudware::entitlement::verifyEmbeddedToken(result.token, fresh, &error)) {
                state.entitlementReadOnly = true;
                state.entitlementStatus = "Received entitlement failed verification";
                resultError = true;
                return;
            }
            if (!result.securityPolicy.empty()) {
                try {
                    const json policy = json::parse(result.securityPolicy);
                    chudware::entitlement::SecurityPolicy verifiedPolicy;
                    std::string policyError;
                    if (!chudware::entitlement::verifyEmbeddedSecurityPolicy(
                            policy.dump(), fresh.subject, verifiedPolicy, &policyError) ||
                        !persistSecurityPolicy(fresh.subject, policy)) {
                        state.entitlementReadOnly = true;
                        state.entitlementStatus = "Security policy could not be stored securely";
                        resultError = true;
                        return;
                    }
                } catch (...) {
                    state.entitlementReadOnly = true;
                    state.entitlementStatus = "Security policy was malformed";
                    resultError = true;
                    return;
                }
            }
            if (!result.enforcementState.empty() &&
                !chudware::security::persistEnforcementState(result.enforcementState, appdataAppDir())) {
                state.entitlementReadOnly = true;
                state.entitlementStatus = "Enforcement state could not be stored securely";
                resultError = true;
                return;
            }
            if (result.operation == Operation::Login &&
                !chudware::device_identity::finalizeAuthorizationKey(appdataAppDir())) {
                state.entitlementReadOnly = true;
                state.entitlementStatus = "Device authorization could not be stored securely";
                resultError = true;
                return;
            }
            c.payload = std::move(fresh);
            c.stamp = {wallNow(), monotonicNow(), bootIdentifier()};
            state.entitlementToken = result.token;
            state.entitlementMutationVerified = false;
            state.entitlementAiVerified = false;
            fillState(state, c.payload);
            if (!persistSubject(c.payload.subject)) {
                state.entitlementReadOnly = true;
                state.entitlementStatus = "Entitlement subject could not be stored securely";
                resultError = true;
                return;
            }
            c.authoritativeRefused = false;
            c.failedAttempts = 0;
            state.entitlementReadOnly = false;
            state.entitlementStatus.clear();
            if (!persist(result.token, c.stamp))
                state.entitlementStatus = "Entitlement is active but could not be stored securely";
        } else if (result.kind == PendingResult::Kind::Refused) {
            c.authoritativeRefused = true;
            c.failedAttempts = 0;
            state.entitlementReadOnly = true;
            state.entitlementStatus = result.detail.empty()
                ? "Desktop entitlement refused" : result.detail;
        } else if (result.kind == PendingResult::Kind::Invalid) {
            c.authoritativeRefused = false;
            c.failedAttempts = 0;
            state.entitlementReadOnly = true;
            state.entitlementSignedIn = false;
            state.entitlementToken.clear();
            state.entitlementMutationVerified = false;
            state.entitlementAiVerified = false;
            state.entitlementStatus = result.detail;
            chudware::secret_store::erase(kService, kAccount, appdataAppDir());
        } else if (state.entitlementSignedIn) {
            ++c.failedAttempts;      // unreachable: keep offline access, retry on the backoff
            evaluateOffline(state);
        } else {
            state.entitlementStatus = result.detail.empty()
                ? "Sign-in service is unreachable" : result.detail;
        }
    });
    if (resultError) return;
    if (c.results.running() != Operation::None) {
        if (state.entitlementSignedIn) {
            const auto decision = chudware::entitlement::offlineAccess(
                c.payload, c.stamp, wallNow(), monotonicNow(), bootIdentifier());
            if (decision.access == chudware::entitlement::OfflineAccess::ClockRollback)
                state.entitlementReadOnly = true;
        }
        return;  // preserve the current operation's progress/status
    }

    bool forceClockCheck = false;
    if (state.entitlementSignedIn && !c.authoritativeRefused) {
        const auto decision = chudware::entitlement::offlineAccess(
            c.payload, c.stamp, wallNow(), monotonicNow(), bootIdentifier());
        evaluateOffline(state);
        forceClockCheck =
            decision.access == chudware::entitlement::OfflineAccess::ClockRollback;
    }
    double retryAfter = kRefreshIntervalSeconds;
    if (forceClockCheck) retryAfter = 60.0;
    else if (c.failedAttempts > 0)
        retryAfter = std::min(kRefreshIntervalSeconds,
                              kRetryBaseSeconds * std::ldexp(1.0, std::min(c.failedAttempts - 1, 16)));
    if (state.entitlementSignedIn && c.results.running() == Operation::None &&
        (c.lastAttemptMonotonic < 0.0 ||
         monotonicNow() - c.lastAttemptMonotonic >= retryAfter))
        startRefresh(state.entitlementToken);
}

void signOutDesktopAccount(AppState& state) {
    Controller& c = controller();
    // Invalidate FIRST. Workers can only publish responses, never recreate erased
    // credentials or acknowledge security events after this UI-owned boundary.
    c.results.invalidate();
    chudware::device_identity::discardAuthorizationKey(appdataAppDir());
    // Everything device-bound about the session: the sealed token, the subject, and the
    // signed security policy. The device authorization key stays — it is device identity,
    // not session state, and the next sign-in re-attests with it.
    chudware::secret_store::erase(kService, kAccount, appdataAppDir());
    chudware::secret_store::erase(kService, kSubjectAccount, appdataAppDir());
    chudware::secret_store::erase(kService, kSecurityAccount, appdataAppDir());
    c.payload = {};
    c.stamp = {};
    c.authoritativeRefused = false;
    c.lastAttemptMonotonic = -1.0;
    state.entitlementToken.clear();
    state.entitlementPlan.clear();
    state.entitlementEmail.clear();
    state.entitlementSignedIn = false;
    state.entitlementReadOnly = true;
    state.entitlementMutationVerified = false;
    state.entitlementAiVerified = false;
    state.entitlementStatus = "Signed out — sign in to activate desktop editing";
}

void drawAccountSettings(AppState& state) {
    ImGui::Text("Status: %s", state.entitlementSignedIn
        ? (state.entitlementReadOnly ? "Read-only" : "Active") : "Not signed in");
    if (state.entitlementSignedIn) {
        ImGui::Text("%s · %s", state.entitlementEmail.c_str(), state.entitlementPlan.c_str());
        if (ImGui::Button("Refresh entitlement"))
            startRefresh(state.entitlementToken);
        ImGui::SameLine();
    }
    if (ImGui::Button(state.entitlementSignedIn ? "Sign in again" : "Sign in"))
        beginDesktopAccountLogin(state);
    ImGui::SameLine();
    if (ImGui::Button("Manage subscription"))
        openExternalHttpUrl("https://app.chudware.com/account/billing");
    if (state.entitlementSignedIn) {
        ImGui::SameLine();
        if (ImGui::Button("Sign out")) signOutDesktopAccount(state);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Forgets this device's stored subscription token. Files stay "
                              "openable and exportable; editing needs a sign-in again.");
    }
    if (!state.entitlementStatus.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(
            state.entitlementReadOnly ? palette.statusWarn : palette.textDim));
        ImGui::TextWrapped("%s", state.entitlementStatus.c_str());
        ImGui::PopStyleColor();
    }
}

// The read-only entitlement notice is RESERVED CHROME (see account_panel.h). It used to be a
// decoration-less floating window pinned inside ui::overlayBand(); every refinement of WHERE to
// pin it just moved which panel it covered next — the Manufacture ribbon, then the document tab
// strip, then the PLM hub's breadcrumb/Refresh/search row. An overlay cannot be positioned
// correctly for all nine workspaces because they do not agree on what lives at the top-left.
// Reserving a strip between the ribbon and the dockspace is the fix that generalizes: the
// dockspace, the doc-tab strip and both pinned columns all start beneath it, so it can never
// cover anything, and the safety-relevant message can never be scrolled or collapsed away.

float entitlementBannerHeight(const AppState& state) {
    if (!state.entitlementReadOnly) return 0.0f;
    // One text line for the headline, one dim line for the status detail (when present), the
    // action buttons, and the strip's own vertical padding. Derived from live font/frame metrics
    // so it tracks DPI and font-size changes instead of drifting against them.
    const float padY = 7.0f * uiScale;
    const float lines = ImGui::GetTextLineHeight() +
                        (state.entitlementStatus.empty() ? 0.0f
                                                         : ImGui::GetTextLineHeightWithSpacing());
    return padY * 2.0f + std::max(lines, ImGui::GetFrameHeight());
}

void drawEntitlementBanner(AppState& state, float x, float y, float w, float h) {
    if (h <= 0.0f || !state.entitlementReadOnly) return;

    const float padX = 12.0f * uiScale;
    const float padY = 7.0f * uiScale;
    ImDrawList* fg = ImGui::GetBackgroundDrawList();
    const ImVec2 p0(x, y), p1(x + w, y + h);

    // Warning-toned wash rather than plain chrome: this is an account state the user needs to
    // notice, but it is not an error and must not read as one. Alpha kept low so the strip reads
    // as chrome tint, not as a modal band.
    float bg[4];
    Palette::withAlpha(palette.statusWarn, bg, 0.14f);
    fg->AddRectFilled(p0, p1, Palette::toU32(bg));
    float rule[4];
    Palette::withAlpha(palette.statusWarn, rule, 0.55f);
    fg->AddLine(ImVec2(p0.x, p1.y - 1.0f), ImVec2(p1.x, p1.y - 1.0f), Palette::toU32(rule),
                std::max(1.0f, uiScale));

    ImGui::SetNextWindowPos(p0, ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(w, h), ImGuiCond_Always);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(padX, padY));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
    if (ImGui::Begin("##entitlement_readonly_strip", nullptr,
                     ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoSavedSettings |
                     ImGuiWindowFlags_NoNav | ImGuiWindowFlags_NoBringToFrontOnFocus |
                     ImGuiWindowFlags_NoScrollWithMouse)) {
        // Actions are laid out from the RIGHT edge first so the headline gets whatever width is
        // left and elides into it, instead of the buttons being pushed off a narrow window.
        const float gap = 8.0f * uiScale;
        const float pad2 = ImGui::GetStyle().FramePadding.x * 2;
        const bool canSignOut = state.entitlementSignedIn;
        const char* signLabel = canSignOut ? "Sign in again" : "Sign in";
        const float signW = ImGui::CalcTextSize(signLabel).x + pad2;
        const float mgmtW = ImGui::CalcTextSize("Manage subscription").x + pad2;
        const float outW = canSignOut ? ImGui::CalcTextSize("Sign out").x + pad2 + gap : 0.0f;
        const float actionsW = signW + mgmtW + gap + outW;
        const float textW = std::max(80.0f * uiScale, w - padX * 2.0f - actionsW - gap * 2.0f);

        ImGui::BeginGroup();
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + textW);
        ImGui::TextUnformatted("Read-only — your files remain openable and exportable");
        if (!state.entitlementStatus.empty())
            ImGui::TextDisabled("%s", state.entitlementStatus.c_str());
        ImGui::PopTextWrapPos();
        ImGui::EndGroup();

        // Every action sits on ONE centered baseline. SameLine() alone does NOT hold it:
        // it restores the cursor to the PREVIOUS line's y — here the multi-line text group's
        // last line — so the second and third buttons drifted several pixels above the first
        // (measured 6 px at uiScale 1.0). Re-assert the row y before each button instead.
        const float rowY = (h - ImGui::GetFrameHeight()) * 0.5f;
        ImGui::SameLine();
        ImGui::SetCursorPos(ImVec2(w - padX - actionsW, rowY));
        if (ImGui::Button(signLabel)) beginDesktopAccountLogin(state);
        ImGui::SameLine(0.0f, gap);
        ImGui::SetCursorPosY(rowY);
        if (ImGui::Button("Manage subscription"))
            openExternalHttpUrl("https://app.chudware.com/account/billing");
        if (canSignOut) {
            ImGui::SameLine(0.0f, gap);
            ImGui::SetCursorPosY(rowY);
            if (ImGui::Button("Sign out")) signOutDesktopAccount(state);
        }
    }
    ImGui::End();
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(2);
}

}  // namespace ui
