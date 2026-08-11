#include "account_panel.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "../core/app_state.h"
#include "../ui_theme.h"
#include "onboarding.h"
#include "web_capabilities.h"
#include "agent/http.h"
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
constexpr const char* kAccount = "entitlement";
constexpr double kRefreshIntervalSeconds = 12.0 * 60.0 * 60.0;

struct PendingResult {
    enum class Kind { None, Fresh, Refused, Unreachable, Invalid } kind = Kind::None;
    std::string token;
    std::string detail;
};

struct Controller {
    std::mutex mutex;
    std::thread worker;
    std::atomic<bool> stopping{false};
    PendingResult pending;
    chudware::entitlement::TokenPayload payload;
    chudware::entitlement::CheckinStamp stamp;
    double lastAttemptMonotonic = -1.0;
    bool initialized = false;
    bool refreshRunning = false;
    bool loginRunning = false;
    bool authoritativeRefused = false;

    ~Controller() {
        stopping.store(true);
        if (worker.joinable()) worker.join();
    }
};

Controller& controller() {
    static Controller value;
    return value;
}

double monotonicNow() {
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

std::int64_t wallNow() {
    return duration_cast<seconds>(system_clock::now().time_since_epoch()).count();
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

std::string stableDeviceMaterial() {
    std::string material = deviceName();
    material.push_back('\n');
#if defined(_WIN32)
    if (const char* user = std::getenv("USERNAME")) material += user;
#else
    if (const char* user = std::getenv("USER")) material += user;
#endif
    material.push_back('\n');
    material += appdataAppDir();
    return material;
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
    };
    const auto key = chudware::entitlement::deviceKey(stableDeviceMaterial());
    const auto sealed = chudware::entitlement::sealForDevice(stored.dump(), key);
    return !sealed.empty() && chudware::secret_store::store(
        kService, kAccount, sealed, appdataAppDir());
}

bool loadStored(std::string& token, chudware::entitlement::CheckinStamp& stamp) {
    std::vector<unsigned char> sealed;
    if (!chudware::secret_store::load(kService, kAccount, sealed, appdataAppDir())) return false;
    std::string plaintext;
    if (!chudware::entitlement::openForDevice(
            sealed, chudware::entitlement::deviceKey(stableDeviceMaterial()), plaintext))
        return false;
    try {
        const json stored = json::parse(plaintext);
        token = stored.at("token").get<std::string>();
        stamp.wallSeconds = stored.at("last_successful_checkin_wall").get<std::int64_t>();
        stamp.monotonicSeconds = stored.at("last_successful_checkin_monotonic").get<double>();
        return !token.empty() && stamp.wallSeconds > 0;
    } catch (const std::exception&) {
        return false;
    }
}

void fillState(AppState& state, const chudware::entitlement::TokenPayload& payload) {
    state.entitlementSignedIn = true;
    state.entitlementPlan = payload.plan;
    state.entitlementEmail = payload.email;
}

void evaluateOffline(AppState& state) {
    Controller& c = controller();
    const auto decision = chudware::entitlement::offlineAccess(
        c.payload, c.stamp, wallNow(), monotonicNow());
    state.entitlementGraceExpiring =
        decision.access == chudware::entitlement::OfflineAccess::Expiring;
    state.entitlementReadOnly =
        decision.access == chudware::entitlement::OfflineAccess::Expired ||
        decision.access == chudware::entitlement::OfflineAccess::ClockRollback;
    if (decision.access == chudware::entitlement::OfflineAccess::ClockRollback)
        state.entitlementStatus = "Clock changed; reconnect to verify your subscription";
    else if (decision.access == chudware::entitlement::OfflineAccess::Expired)
        state.entitlementStatus = "Offline entitlement expired";
    else if (decision.access == chudware::entitlement::OfflineAccess::Expiring)
        state.entitlementStatus = "Subscription check unavailable — reconnect within 3 days";
    else
        state.entitlementStatus.clear();
}

std::string refreshUrl() {
    if (const char* overrideUrl = std::getenv("CHUDWARE_ENTITLEMENT_REFRESH_URL");
        overrideUrl && *overrideUrl)
        return overrideUrl;
    return "https://app.chudware.com/api/entitlement/refresh";
}

void setPending(PendingResult result) {
    Controller& c = controller();
    std::lock_guard<std::mutex> lock(c.mutex);
    c.pending = std::move(result);
    c.refreshRunning = false;
    c.loginRunning = false;
}

std::string randomState();

void startRefresh(const std::string& token) {
    Controller& c = controller();
    if (c.refreshRunning || c.loginRunning || c.worker.joinable()) return;
    c.refreshRunning = true;
    c.lastAttemptMonotonic = monotonicNow();
    const std::string challenge = randomState();
    c.worker = std::thread([token, challenge]() {
        const auto response = chudware::agent::http_post(
            refreshUrl(), {{"Content-Type", "application/chudware-entitlement"},
                           {"X-Chudware-Refresh-Nonce", challenge}}, token, 8000);
        if (response.status == 200) {
            try {
                const json body = json::parse(response.body);
                if (body.value("entitled", false) && body.contains("token") && body["token"].is_string()) {
                    chudware::entitlement::TokenPayload fresh;
                    std::string error;
                    const std::string freshToken = body["token"].get<std::string>();
                    if (chudware::entitlement::verifyEmbeddedToken(freshToken, fresh, &error)) {
                        setPending({PendingResult::Kind::Fresh, freshToken, {}});
                        return;
                    }
                    setPending({PendingResult::Kind::Invalid, {}, "Gateway returned an invalid entitlement"});
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
                setPending({PendingResult::Kind::Refused, {}, reason});
                return;
            }
            setPending({PendingResult::Kind::Unreachable, {}, "Subscription check returned an untrusted response"});
            return;
        }
        if (response.status == 0 || response.status >= 500) {
            setPending({PendingResult::Kind::Unreachable, {}, response.error});
            return;
        }
        // Unsigned 4xx responses are not authoritative refusals. Treat them like
        // an unreachable gateway so a hostile network cannot force read-only.
        setPending({PendingResult::Kind::Unreachable, {}, "Subscription check could not be authenticated"});
    });
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
#if defined(_WIN32)
    DWORD timeout = 1000;
    setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&timeout), sizeof(timeout));
#else
    timeval timeout{1, 0};
    setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
#endif
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

void sendBrowserResponse(Socket client, bool ok) {
    const std::string body = ok
        ? "<!doctype html><title>Chudware authorized</title><h1>Authorized</h1><p>You can close this tab and return to Chudware.</p>"
        : "<!doctype html><title>Authorization failed</title><h1>Authorization failed</h1><p>Return to Chudware and try again.</p>";
    const std::string response = std::string("HTTP/1.1 ") + (ok ? "200 OK" : "400 Bad Request") +
        "\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\nContent-Length: " +
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

    std::string token;
    if (!loadStored(token, c.stamp) || !launchSignatureValid(token)) {
        chudware::secret_store::erase(kService, kAccount, appdataAppDir());
        state.entitlementReadOnly = true;
        state.entitlementSignedIn = false;
        state.entitlementStatus = "Sign in to activate desktop editing";
        return;
    }
    std::string error;
    if (!chudware::entitlement::verifyEmbeddedToken(token, c.payload, &error)) {
        chudware::secret_store::erase(kService, kAccount, appdataAppDir());
        state.entitlementReadOnly = true;
        state.entitlementSignedIn = false;
        state.entitlementStatus = "Stored entitlement was damaged; sign in again";
        return;
    }
    state.entitlementToken = token;
    fillState(state, c.payload);
    evaluateOffline(state);
    startRefresh(token);
}

bool beginDesktopAccountLogin(AppState& state) {
    Controller& c = controller();
    if (c.loginRunning || c.refreshRunning || c.worker.joinable()) return false;
    unsigned short port = 0;
    Socket listener = makeListener(port);
    if (listener == kInvalidSocket) {
        state.entitlementStatus = "Could not start the secure loopback sign-in listener";
        return false;
    }
    const std::string stateValue = randomState();
    const std::string device = deviceName();
    const char* baseEnv = std::getenv("CHUDWARE_ENTITLEMENT_AUTHORIZE_URL");
    const std::string base = baseEnv && *baseEnv
        ? baseEnv : "https://app.chudware.com/desktop/authorize";
    const std::string url = base + "?port=" + std::to_string(port) +
        "&state=" + urlEncode(stateValue) + "&device=" + urlEncode(device);
    if (!openExternalHttpUrl(url)) {
        closeSocket(listener);
        state.entitlementStatus = "Could not open the system browser";
        return false;
    }
    c.loginRunning = true;
    state.entitlementStatus = "Waiting for browser authorization";
    c.worker = std::thread([listener, stateValue]() {
        const double deadline = monotonicNow() + 300.0;
        while (!controller().stopping.load() && monotonicNow() < deadline) {
            sockaddr_in peer{};
            socklen_t peerLength = sizeof(peer);
            Socket client = ::accept(listener, reinterpret_cast<sockaddr*>(&peer), &peerLength);
            if (client == kInvalidSocket) continue;
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
                    if (ok) setPending({PendingResult::Kind::Fresh, token, {}});
                }
            }
            sendBrowserResponse(client, ok);
            closeSocket(client);
            if (ok) {
                closeSocket(listener);
                return;
            }
        }
        closeSocket(listener);
        setPending({PendingResult::Kind::Unreachable, {}, "Browser authorization timed out"});
    });
    return true;
}

void tickAccountPanel(AppState& state) {
    Controller& c = controller();
    if (c.worker.joinable()) {
        PendingResult result;
        {
            std::lock_guard<std::mutex> lock(c.mutex);
            if (c.pending.kind == PendingResult::Kind::None) {
                if (state.entitlementSignedIn) {
                    const auto decision = chudware::entitlement::offlineAccess(
                        c.payload, c.stamp, wallNow(), monotonicNow());
                    if (decision.access == chudware::entitlement::OfflineAccess::ClockRollback)
                        state.entitlementReadOnly = true;
                }
                return;
            }
            result = std::move(c.pending);
            c.pending = {};
        }
        c.worker.join();
        if (result.kind == PendingResult::Kind::Fresh) {
            chudware::entitlement::TokenPayload fresh;
            std::string error;
            if (!chudware::entitlement::verifyEmbeddedToken(result.token, fresh, &error)) {
                state.entitlementReadOnly = true;
                state.entitlementStatus = "Received entitlement failed verification";
                return;
            }
            c.payload = std::move(fresh);
            c.stamp = {wallNow(), monotonicNow()};
            state.entitlementToken = result.token;
            state.entitlementMutationVerified = false;
            state.entitlementAiVerified = false;
            fillState(state, c.payload);
            c.authoritativeRefused = false;
            state.entitlementReadOnly = false;
            state.entitlementGraceExpiring = false;
            state.entitlementStatus.clear();
            if (!persist(result.token, c.stamp))
                state.entitlementStatus = "Entitlement is active but could not be stored securely";
        } else if (result.kind == PendingResult::Kind::Refused) {
            c.authoritativeRefused = true;
            state.entitlementReadOnly = true;
            state.entitlementGraceExpiring = false;
            state.entitlementStatus = result.detail.empty()
                ? "Desktop entitlement refused" : result.detail;
        } else if (result.kind == PendingResult::Kind::Invalid) {
            c.authoritativeRefused = false;
            state.entitlementReadOnly = true;
            state.entitlementSignedIn = false;
            state.entitlementToken.clear();
            state.entitlementMutationVerified = false;
            state.entitlementAiVerified = false;
            state.entitlementStatus = result.detail;
            chudware::secret_store::erase(kService, kAccount, appdataAppDir());
        } else if (state.entitlementSignedIn) {
            evaluateOffline(state);  // unreachable: grace, including last-three-days chip
        } else {
            state.entitlementStatus = result.detail.empty()
                ? "Sign-in service is unreachable" : result.detail;
        }
    }

    bool forceClockCheck = false;
    if (state.entitlementSignedIn && !c.authoritativeRefused) {
        const auto decision = chudware::entitlement::offlineAccess(
            c.payload, c.stamp, wallNow(), monotonicNow());
        evaluateOffline(state);
        forceClockCheck =
            decision.access == chudware::entitlement::OfflineAccess::ClockRollback;
    }
    const double retryAfter = forceClockCheck ? 60.0 : kRefreshIntervalSeconds;
    if (state.entitlementSignedIn && !c.refreshRunning && !c.loginRunning &&
        !c.worker.joinable() &&
        (c.lastAttemptMonotonic < 0.0 ||
         monotonicNow() - c.lastAttemptMonotonic >= retryAfter))
        startRefresh(state.entitlementToken);
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
    if (!state.entitlementStatus.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(
            state.entitlementReadOnly ? palette.statusWarn : palette.textDim));
        ImGui::TextWrapped("%s", state.entitlementStatus.c_str());
        ImGui::PopStyleColor();
    }
}

void drawEntitlementBanner(AppState& state) {
    if (!state.entitlementReadOnly && !state.entitlementGraceExpiring) return;
    ImGuiViewport* viewport = ImGui::GetMainViewport();
    if (state.entitlementGraceExpiring && !state.entitlementReadOnly) {
        ImGui::SetNextWindowPos(ImVec2(viewport->WorkPos.x + viewport->WorkSize.x - 12.0f * uiScale,
                                      viewport->WorkPos.y + viewport->WorkSize.y - 12.0f * uiScale),
                                ImGuiCond_Always, ImVec2(1.0f, 1.0f));
        ImGui::SetNextWindowBgAlpha(0.88f);
        if (ImGui::Begin("##entitlement_grace_chip", nullptr,
                         ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoDecoration |
                         ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoNav)) {
            ImGui::TextUnformatted("Subscription check unavailable — reconnect within 3 days");
        }
        ImGui::End();
        return;
    }

    ImGui::SetNextWindowPos(ImVec2(viewport->WorkPos.x + viewport->WorkSize.x * 0.5f,
                                  viewport->WorkPos.y + 8.0f * uiScale),
                            ImGuiCond_Always, ImVec2(0.5f, 0.0f));
    ImGui::SetNextWindowBgAlpha(0.94f);
    if (ImGui::Begin("##entitlement_readonly_banner", nullptr,
                     ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoDecoration |
                     ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoNav)) {
        ImGui::TextUnformatted("Read-only — your files remain openable and exportable");
        if (!state.entitlementStatus.empty()) {
            ImGui::SameLine();
            ImGui::TextDisabled("%s", state.entitlementStatus.c_str());
        }
        if (ImGui::Button("Sign in")) beginDesktopAccountLogin(state);
        ImGui::SameLine();
        if (ImGui::Button("Manage subscription"))
            openExternalHttpUrl("https://app.chudware.com/account/billing");
    }
    ImGui::End();
}

}  // namespace ui
