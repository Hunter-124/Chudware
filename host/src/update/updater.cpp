#include "updater.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <mutex>
#include <optional>
#include <sstream>
#include <thread>
#include <unordered_set>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "agent/http.h"
#include "util/entitlement.h"
extern "C" {
#include "util/tweetnacl.h"
}

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <winhttp.h>
#else
#include <curl/curl.h>
#include <unistd.h>
#endif

namespace chudware::update {
namespace {

using json = nlohmann::json;
using Clock = std::chrono::steady_clock;
constexpr auto kCheckInterval = std::chrono::hours(24);
constexpr auto kInitialBackoff = std::chrono::minutes(5);
constexpr auto kMaxBackoff = std::chrono::hours(6);

struct SemVer {
    std::uint64_t major = 0, minor = 0, patch = 0;
    std::vector<std::string> prerelease;
};

bool parseNumber(std::string_view value, std::uint64_t& out) {
    if (value.empty() || (value.size() > 1 && value.front() == '0')) return false;
    out = 0;
    for (const char c : value) {
        if (c < '0' || c > '9') return false;
        const unsigned digit = static_cast<unsigned>(c - '0');
        if (out > (UINT64_MAX - digit) / 10) return false;
        out = out * 10 + digit;
    }
    return true;
}

bool validIdentifier(std::string_view value, bool prerelease) {
    if (value.empty()) return false;
    bool numeric = true;
    for (const char c : value) {
        if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '-')) return false;
        numeric = numeric && c >= '0' && c <= '9';
    }
    return !(prerelease && numeric && value.size() > 1 && value.front() == '0');
}

std::optional<SemVer> parseVersion(std::string_view value) {
    const std::size_t plus = value.find('+');
    std::string_view coreAndPre = value.substr(0, plus);
    if (plus != std::string_view::npos) {
        std::string_view build = value.substr(plus + 1);
        while (!build.empty()) {
            const std::size_t dot = build.find('.');
            if (!validIdentifier(build.substr(0, dot), false)) return std::nullopt;
            if (dot == std::string_view::npos) break;
            build.remove_prefix(dot + 1);
        }
    }
    SemVer out;
    const std::size_t dash = coreAndPre.find('-');
    std::string_view core = coreAndPre.substr(0, dash);
    std::array<std::uint64_t*, 3> numbers{&out.major, &out.minor, &out.patch};
    for (std::size_t i = 0; i < numbers.size(); ++i) {
        const std::size_t dot = core.find('.');
        const std::string_view part = core.substr(0, dot);
        if (!parseNumber(part, *numbers[i])) return std::nullopt;
        if (i < 2) {
            if (dot == std::string_view::npos) return std::nullopt;
            core.remove_prefix(dot + 1);
        } else if (dot != std::string_view::npos) {
            return std::nullopt;
        }
    }
    if (dash != std::string_view::npos) {
        std::string_view pre = coreAndPre.substr(dash + 1);
        while (!pre.empty()) {
            const std::size_t dot = pre.find('.');
            const std::string_view part = pre.substr(0, dot);
            if (!validIdentifier(part, true)) return std::nullopt;
            out.prerelease.emplace_back(part);
            if (dot == std::string_view::npos) break;
            pre.remove_prefix(dot + 1);
        }
        if (out.prerelease.empty()) return std::nullopt;
    }
    return out;
}

bool isNumeric(std::string_view value) {
    return !value.empty() && std::all_of(value.begin(), value.end(),
        [](char c) { return c >= '0' && c <= '9'; });
}

int compareParsed(const SemVer& lhs, const SemVer& rhs) {
    const std::array<std::pair<std::uint64_t, std::uint64_t>, 3> numbers{{
        {lhs.major, rhs.major}, {lhs.minor, rhs.minor}, {lhs.patch, rhs.patch}}};
    for (const auto [a, b] : numbers) {
        if (a != b) return a < b ? -1 : 1;
    }
    if (lhs.prerelease.empty() != rhs.prerelease.empty())
        return lhs.prerelease.empty() ? 1 : -1;
    const std::size_t count = std::min(lhs.prerelease.size(), rhs.prerelease.size());
    for (std::size_t i = 0; i < count; ++i) {
        const std::string& a = lhs.prerelease[i];
        const std::string& b = rhs.prerelease[i];
        if (a == b) continue;
        const bool an = isNumeric(a), bn = isNumeric(b);
        if (an != bn) return an ? -1 : 1;
        if (an) {
            if (a.size() != b.size()) return a.size() < b.size() ? -1 : 1;
        }
        return a < b ? -1 : 1;
    }
    if (lhs.prerelease.size() == rhs.prerelease.size()) return 0;
    return lhs.prerelease.size() < rhs.prerelease.size() ? -1 : 1;
}

// Small allocation-free SHA-256. Update artifacts are streamed from disk; no
// cryptographic library or whole-artifact buffer is introduced into the host.
class Sha256 {
public:
    void update(const unsigned char* bytes, std::size_t count) {
        total_ += count;
        while (count) {
            const std::size_t take = std::min(count, block_.size() - used_);
            std::memcpy(block_.data() + used_, bytes, take);
            used_ += take;
            bytes += take;
            count -= take;
            if (used_ == block_.size()) {
                transform(block_.data());
                used_ = 0;
            }
        }
    }

    std::array<unsigned char, 32> finish() {
        const std::uint64_t bits = total_ * 8;
        block_[used_++] = 0x80;
        if (used_ > 56) {
            std::fill(block_.begin() + static_cast<std::ptrdiff_t>(used_), block_.end(), 0);
            transform(block_.data());
            used_ = 0;
        }
        std::fill(block_.begin() + static_cast<std::ptrdiff_t>(used_), block_.begin() + 56, 0);
        for (int i = 0; i < 8; ++i)
            block_[63 - i] = static_cast<unsigned char>(bits >> (i * 8));
        transform(block_.data());
        std::array<unsigned char, 32> out{};
        for (std::size_t i = 0; i < state_.size(); ++i) {
            out[i * 4] = static_cast<unsigned char>(state_[i] >> 24);
            out[i * 4 + 1] = static_cast<unsigned char>(state_[i] >> 16);
            out[i * 4 + 2] = static_cast<unsigned char>(state_[i] >> 8);
            out[i * 4 + 3] = static_cast<unsigned char>(state_[i]);
        }
        return out;
    }

private:
    static std::uint32_t rotate(std::uint32_t value, unsigned count) {
        return (value >> count) | (value << (32 - count));
    }
    void transform(const unsigned char* input) {
        static constexpr std::array<std::uint32_t, 64> k{{
            0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
            0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
            0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
            0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
            0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
            0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
            0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
            0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2}};
        std::uint32_t w[64];
        for (int i = 0; i < 16; ++i)
            w[i] = (static_cast<std::uint32_t>(input[i * 4]) << 24) |
                   (static_cast<std::uint32_t>(input[i * 4 + 1]) << 16) |
                   (static_cast<std::uint32_t>(input[i * 4 + 2]) << 8) |
                   static_cast<std::uint32_t>(input[i * 4 + 3]);
        for (int i = 16; i < 64; ++i) {
            const std::uint32_t s0 = rotate(w[i - 15], 7) ^ rotate(w[i - 15], 18) ^ (w[i - 15] >> 3);
            const std::uint32_t s1 = rotate(w[i - 2], 17) ^ rotate(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        auto [a,b,c,d,e,f,g,h] = std::array<std::uint32_t,8>{state_[0],state_[1],state_[2],state_[3],state_[4],state_[5],state_[6],state_[7]};
        for (int i = 0; i < 64; ++i) {
            const std::uint32_t s1 = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25);
            const std::uint32_t choice = (e & f) ^ (~e & g);
            const std::uint32_t t1 = h + s1 + choice + k[i] + w[i];
            const std::uint32_t s0 = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22);
            const std::uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
            const std::uint32_t t2 = s0 + majority;
            h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        state_[0]+=a; state_[1]+=b; state_[2]+=c; state_[3]+=d;
        state_[4]+=e; state_[5]+=f; state_[6]+=g; state_[7]+=h;
    }

    std::array<std::uint32_t, 8> state_{{0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                                         0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19}};
    std::array<unsigned char, 64> block_{};
    std::size_t used_ = 0;
    std::uint64_t total_ = 0;
};

std::string hexDigest(const std::array<unsigned char, 32>& digest) {
    static constexpr char hex[] = "0123456789abcdef";
    std::string out(64, '0');
    for (std::size_t i = 0; i < digest.size(); ++i) {
        out[i * 2] = hex[digest[i] >> 4];
        out[i * 2 + 1] = hex[digest[i] & 15];
    }
    return out;
}

std::vector<unsigned char> decodeBase64(std::string_view value) {
    auto cv = [](unsigned char c) -> int {
        if (c >= 'A' && c <= 'Z') return c - 'A';
        if (c >= 'a' && c <= 'z') return c - 'a' + 26;
        if (c >= '0' && c <= '9') return c - '0' + 52;
        if (c == '+') return 62;
        if (c == '/') return 63;
        return -1;
    };
    if (value.empty() || value.size() % 4 != 0) return {};
    std::vector<unsigned char> out;
    out.reserve(value.size() / 4 * 3);
    for (std::size_t i = 0; i < value.size(); i += 4) {
        int a = cv(value[i]), b = cv(value[i + 1]);
        int c = value[i + 2] == '=' ? -2 : cv(value[i + 2]);
        int d = value[i + 3] == '=' ? -2 : cv(value[i + 3]);
        if (a < 0 || b < 0 || c == -1 || d == -1 || (c == -2 && d != -2) ||
            (i + 4 != value.size() && (c == -2 || d == -2))) return {};
        const std::uint32_t word = (static_cast<std::uint32_t>(a) << 18) |
            (static_cast<std::uint32_t>(b) << 12) |
            (static_cast<std::uint32_t>(c < 0 ? 0 : c) << 6) |
            static_cast<std::uint32_t>(d < 0 ? 0 : d);
        out.push_back(static_cast<unsigned char>(word >> 16));
        if (c >= 0) out.push_back(static_cast<unsigned char>(word >> 8));
        if (d >= 0) out.push_back(static_cast<unsigned char>(word));
    }
    return out;
}

bool signatureValid(const std::string& sha256, const std::string& encoded) {
    const std::vector<unsigned char> signature = decodeBase64(encoded);
    if (signature.size() != crypto_sign_BYTES || sha256.size() != 64) return false;
    std::vector<unsigned char> signedMessage(signature.size() + sha256.size());
    std::copy(signature.begin(), signature.end(), signedMessage.begin());
    std::copy(sha256.begin(), sha256.end(), signedMessage.begin() + signature.size());
    std::vector<unsigned char> opened(signedMessage.size());
    unsigned long long openedLength = 0;
    return crypto_sign_open(opened.data(), &openedLength, signedMessage.data(),
                            static_cast<unsigned long long>(signedMessage.size()),
                            entitlement::kEmbeddedPublicKey.data()) == 0 &&
           openedLength == sha256.size() &&
           std::equal(opened.begin(), opened.begin() + static_cast<std::ptrdiff_t>(openedLength),
                      sha256.begin());
}

std::string platformName() {
#if defined(_WIN32)
    return "windows";
#elif defined(__APPLE__)
    return "macos";
#else
    return "linux";
#endif
}

std::string architectureName() {
#if defined(__aarch64__) || defined(_M_ARM64)
    return "arm64";
#else
    return "x86_64";
#endif
}

std::string artifactName(const std::string& version) {
#if defined(_WIN32)
    return "Chudware-Setup-" + version + "-x86_64.exe";
#elif defined(__APPLE__)
    return "Chudware-" + version + "-macos-" + architectureName() + ".zip";
#else
    return "Chudware-" + version + "-linux-x86_64.AppImage";
#endif
}

bool manifestFromJson(const std::string& body, Manifest& out, std::string& error) {
    try {
        const json value = json::parse(body);
        out.version = value.at("version").get<std::string>();
        out.minSupported = value.at("min_supported").get<std::string>();
        out.sha256 = value.at("sha256").get<std::string>();
        out.size = value.at("size").get<std::uint64_t>();
        out.signature = value.at("signature").get<std::string>();
        out.notesUrl = value.at("notes_url").get<std::string>();
        out.url = value.value("url", std::string());
        if (!parseVersion(out.version) || !parseVersion(out.minSupported) ||
            out.sha256.size() != 64 || out.size == 0 || out.signature.empty())
            throw std::runtime_error("invalid manifest fields");
        if (!std::all_of(out.sha256.begin(), out.sha256.end(), [](char c) {
                return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
            })) throw std::runtime_error("invalid manifest digest");
        return true;
    } catch (const std::exception& exc) {
        error = std::string("invalid release manifest: ") + exc.what();
        return false;
    }
}

bool canReplaceInstall(const std::filesystem::path& executable) {
#if defined(_WIN32)
    (void)executable;
    return true;
#else
    std::filesystem::path installed = executable;
#if defined(__linux__)
    if (const char* appImage = std::getenv("APPIMAGE"); appImage && *appImage)
        installed = appImage;
#elif defined(__APPLE__)
    installed = executable.parent_path().parent_path().parent_path();
#endif
    if (installed.empty()) return false;
    const auto marker = installed.parent_path() /
        (".chudware-update-write-" + std::to_string(static_cast<long long>(::getpid())));
    std::ofstream out(marker, std::ios::binary | std::ios::trunc);
    const bool ok = static_cast<bool>(out);
    out.close();
    std::error_code ec;
    std::filesystem::remove(marker, ec);
    return ok;
#endif
}

}  // namespace

int compareVersions(const std::string& lhs, const std::string& rhs) {
    const auto a = parseVersion(lhs), b = parseVersion(rhs);
    if (!a || !b) return a ? 1 : b ? -1 : 0;
    return compareParsed(*a, *b);
}

bool requiresMandatoryUpdate(const std::string& current,
                             const std::string& minSupported) {
    return compareVersions(current, minSupported) < 0;
}

struct Updater::Impl {
    mutable std::mutex mutex;
    std::condition_variable wake;
    std::thread worker;
    bool configured = false;
    bool running = false;
    bool stopping = false;
    bool forceCheck = false;
    bool mandatoryActive = false;
    std::string currentVersion;
    std::filesystem::path dataDir;
    std::filesystem::path executablePath;
    std::string token;
    Snapshot status;
    std::unordered_set<std::string> reported;

    std::filesystem::path updateDir() const { return dataDir / "updates"; }
    std::filesystem::path reportsPath() const { return updateDir() / "reported-rejections.txt"; }
    std::filesystem::path stagedMetadata() const { return updateDir() / "staged.json"; }

    void setStatus(State state, const Manifest* manifest, std::string message,
                   std::string url = {}) {
        std::lock_guard<std::mutex> lock(mutex);
        if (state == State::Mandatory) mandatoryActive = true;
        else if (state == State::Current) mandatoryActive = false;
        status.state = mandatoryActive ? State::Mandatory : state;
        if (manifest) status.version = manifest->version;
        status.message = std::move(message);
        status.downloadUrl = std::move(url);
        status.mandatory = mandatoryActive;
    }

    bool stopRequested() const {
        std::lock_guard<std::mutex> lock(mutex);
        return stopping;
    }

    void loadReports() {
        reported.clear();
        std::ifstream in(reportsPath());
        std::string line;
        while (std::getline(in, line)) if (!line.empty()) reported.insert(line);
    }

    bool reportOnce(const Manifest& manifest, const std::string& message) {
        const std::string fingerprint = manifest.sha256 + ":" + manifest.signature;
        std::lock_guard<std::mutex> lock(mutex);
        if (!reported.insert(fingerprint).second) return false;
        std::filesystem::create_directories(updateDir());
        std::ofstream out(reportsPath(), std::ios::app);
        out << fingerprint << '\n';
        ++status.reportSerial;
        status.message = message;
        std::fprintf(stderr, "[update] rejected %s: %s\n",
                     manifest.version.c_str(), message.c_str());
        return true;
    }

    bool verifyFile(const Manifest& manifest, const std::filesystem::path& path,
                    std::string& error, bool eraseOnFailure) {
        std::ifstream in(path, std::ios::binary);
        if (!in) {
            error = "downloaded update could not be opened";
        } else {
            Sha256 sha;
            std::uint64_t size = 0;
            // Stay well below Windows' default 1 MiB thread stack; the former 1 MiB
            // buffer overflowed the stack in the function prologue before any read.
            std::array<unsigned char, 64 * 1024> buffer{};
            while (in) {
                in.read(reinterpret_cast<char*>(buffer.data()), buffer.size());
                const std::streamsize count = in.gcount();
                if (count > 0) {
                    sha.update(buffer.data(), static_cast<std::size_t>(count));
                    size += static_cast<std::uint64_t>(count);
                }
            }
            if (in.bad()) error = "downloaded update could not be read";
            else if (size != manifest.size) error = "downloaded update size does not match manifest";
            else {
                const std::string digest = hexDigest(sha.finish());
                if (digest != manifest.sha256)
                    error = "downloaded update SHA-256 does not match manifest";
                else if (!signatureValid(digest, manifest.signature))
                    error = "downloaded update Ed25519 signature is invalid";
                else
                    return true;
            }
        }
        if (eraseOnFailure) {
            std::error_code ec;
            std::filesystem::remove(path, ec);
        }
        reportOnce(manifest, error);
        return false;
    }

    bool writeStagedMetadata(const Manifest& manifest, const std::filesystem::path& artifact,
                             std::string& error) {
        const json value = {{"version", manifest.version}, {"min_supported", manifest.minSupported},
            {"sha256", manifest.sha256}, {"size", manifest.size},
            {"signature", manifest.signature}, {"notes_url", manifest.notesUrl},
            {"url", manifest.url}, {"artifact", artifact.filename().string()}};
        const auto temporary = stagedMetadata().string() + ".tmp";
        {
            std::ofstream out(temporary, std::ios::binary | std::ios::trunc);
            out << value.dump();
            if (!out) {
                error = "could not persist verified update metadata";
                std::filesystem::remove(temporary);
                return false;
            }
        }
        std::error_code ec;
        std::filesystem::rename(temporary, stagedMetadata(), ec);
        if (ec) {
            std::filesystem::remove(temporary);
            error = "could not atomically stage update metadata: " + ec.message();
            return false;
        }
        return true;
    }

    bool verifyAndStage(const Manifest& manifest, const std::filesystem::path& downloaded,
                        std::string& error) {
        if (!verifyFile(manifest, downloaded, error, true)) return false;
        std::filesystem::create_directories(updateDir());
        const auto staged = updateDir() / artifactName(manifest.version);
        std::error_code ec;
        if (downloaded != staged) {
            std::filesystem::remove(staged, ec);
            ec.clear();
            std::filesystem::rename(downloaded, staged, ec);
            if (ec) {
                error = "could not atomically stage verified update: " + ec.message();
                std::filesystem::remove(downloaded);
                return false;
            }
        }
        if (!writeStagedMetadata(manifest, staged, error)) {
            std::filesystem::remove(staged);
            return false;
        }
        setStatus(State::Staged, &manifest,
                  "Update " + manifest.version + " verified and ready for next launch");
        return true;
    }

#if !defined(_WIN32)
    struct CurlSink {
        std::ofstream* out = nullptr;
        std::uint64_t written = 0;
        std::uint64_t maximum = 0;
        bool overflow = false;
        const Impl* owner = nullptr;
    };
    static std::size_t curlWrite(char* data, std::size_t size, std::size_t count, void* opaque) {
        auto& sink = *static_cast<CurlSink*>(opaque);
        const std::size_t bytes = size * count;
        if (bytes > sink.maximum - sink.written) {
            sink.overflow = true;
            return 0;
        }
        sink.out->write(data, static_cast<std::streamsize>(bytes));
        if (!*sink.out) return 0;
        sink.written += bytes;
        return bytes;
    }
    static int curlProgress(void* opaque, curl_off_t, curl_off_t, curl_off_t, curl_off_t) {
        return static_cast<CurlSink*>(opaque)->owner->stopRequested() ? 1 : 0;
    }
#endif

    bool download(const Manifest& manifest, const std::filesystem::path& destination,
                  std::string& error) {
        std::filesystem::create_directories(destination.parent_path());
        std::ofstream out(destination, std::ios::binary | std::ios::trunc);
        if (!out) {
            error = "could not create temporary update download";
            return false;
        }
#if defined(_WIN32)
        // WinHTTP streaming implementation is kept local so an installer-sized
        // artifact is never accumulated in memory by agent::http_get.
        auto widen = [](const std::string& value) {
            const int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                                              static_cast<int>(value.size()), nullptr, 0);
            std::wstring result(n > 0 ? static_cast<std::size_t>(n) : 0, L'\0');
            if (n > 0) MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                                           static_cast<int>(value.size()), result.data(), n);
            return result;
        };
        struct Handle { HINTERNET h = nullptr; ~Handle(){ if(h) WinHttpCloseHandle(h); }
                        operator HINTERNET() const { return h; } };
        const std::wstring url = widen(manifest.url);
        URL_COMPONENTS uc{}; uc.dwStructSize = sizeof(uc); uc.dwSchemeLength = uc.dwHostNameLength =
            uc.dwUrlPathLength = uc.dwExtraInfoLength = static_cast<DWORD>(-1);
        if (!WinHttpCrackUrl(url.c_str(), static_cast<DWORD>(url.size()), 0, &uc)) {
            error = "WinHttpCrackUrl failed"; return false;
        }
        std::wstring host(uc.lpszHostName, uc.dwHostNameLength);
        std::wstring path(uc.lpszUrlPath, uc.dwUrlPathLength);
        if (uc.dwExtraInfoLength) path.append(uc.lpszExtraInfo, uc.dwExtraInfoLength);
        Handle session{WinHttpOpen(L"chudware-updater/1", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
                                   WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0)};
        Handle connection{session ? WinHttpConnect(session, host.c_str(), uc.nPort, 0) : nullptr};
        Handle request{connection ? WinHttpOpenRequest(connection, L"GET", path.c_str(), nullptr,
            WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
            uc.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0) : nullptr};
        if (!session || !connection || !request ||
            !WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                                WINHTTP_NO_REQUEST_DATA, 0, 0, 0) ||
            !WinHttpReceiveResponse(request, nullptr)) {
            error = "WinHTTP update download failed"; return false;
        }
        DWORD status = 0, statusSize = sizeof(status);
        if (!WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                                 WINHTTP_HEADER_NAME_BY_INDEX, &status, &statusSize,
                                 WINHTTP_NO_HEADER_INDEX) || status != 200) {
            error = "update download returned HTTP " + std::to_string(status); return false;
        }
        std::uint64_t written = 0;
        while (!stopRequested()) {
            DWORD available = 0;
            if (!WinHttpQueryDataAvailable(request, &available)) { error = "WinHTTP read failed"; return false; }
            if (!available) break;
            std::vector<char> chunk(std::min<DWORD>(available, 1024 * 1024));
            DWORD got = 0;
            if (!WinHttpReadData(request, chunk.data(), static_cast<DWORD>(chunk.size()), &got)) {
                error = "WinHTTP read failed"; return false;
            }
            if (written + got > manifest.size) {
                error = "integrity: downloaded update exceeds manifest size";
                return false;
            }
            out.write(chunk.data(), got); written += got;
        }
        if (stopRequested()) { error = "update download cancelled"; return false; }
#else
        CURL* curl = curl_easy_init();
        if (!curl) { error = "libcurl initialization failed"; return false; }
        CurlSink sink{&out, 0, manifest.size, false, this};
        curl_easy_setopt(curl, CURLOPT_URL, manifest.url.c_str());
        curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
        curl_easy_setopt(curl, CURLOPT_MAXREDIRS, 3L);
        curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 10L);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, 600L);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, &curlWrite);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &sink);
        curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, &curlProgress);
        curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &sink);
        curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
        curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "https,http");
        const CURLcode rc = curl_easy_perform(curl);
        long status = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
        curl_easy_cleanup(curl);
        if (rc != CURLE_OK || status < 200 || status >= 300) {
            error = sink.overflow
                ? "integrity: downloaded update exceeds manifest size"
                : rc != CURLE_OK ? curl_easy_strerror(rc)
                                 : "update download returned HTTP " + std::to_string(status);
            out.close();
            std::filesystem::remove(destination);
            return false;
        }
#endif
        out.close();
        if (!out) {
            error = "writing temporary update download failed";
            std::filesystem::remove(destination);
            return false;
        }
        return true;
    }

    bool checkOnce() {
        std::string version, entitlementToken;
        std::filesystem::path executable;
        {
            std::lock_guard<std::mutex> lock(mutex);
            version = currentVersion;
            entitlementToken = token;
            executable = executablePath;
        }
        const char* endpointEnv = std::getenv("CHUDWARE_UPDATE_MANIFEST_URL");
        const char* channelEnv = std::getenv("CHUDWARE_UPDATE_CHANNEL");
        const std::string channel = channelEnv && std::strcmp(channelEnv, "beta") == 0
            ? "beta" : "stable";
        const std::string endpoint = endpointEnv && *endpointEnv
            ? endpointEnv
            : "https://app.chudware.com/api/releases/latest";
        const std::string url = endpoint + "?channel=" + channel + "&platform=" +
            platformName() + "&arch=" + architectureName();
        std::vector<std::pair<std::string, std::string>> headers;
        if (!entitlementToken.empty()) headers.emplace_back("X-Chudware-Entitlement", entitlementToken);
        const auto response = agent::http_get(url, headers, 8000);
        if (response.status != 200) {
            setStatus(State::Error, nullptr, response.status
                ? "Update check returned HTTP " + std::to_string(response.status)
                : "Update check failed: " + response.error);
            return false;
        }
        Manifest manifest;
        std::string error;
        if (!manifestFromJson(response.body, manifest, error)) {
            setStatus(State::Error, nullptr, error);
            return false;
        }
        if (requiresMandatoryUpdate(version, manifest.minSupported)) {
            setStatus(State::Mandatory, &manifest,
                "Chudware " + manifest.minSupported + " or newer is required",
                manifest.url.empty() ? manifest.notesUrl : manifest.url);
            if (manifest.url.empty()) return true;
        } else if (compareVersions(version, manifest.version) >= 0) {
            setStatus(State::Current, &manifest, "Chudware is up to date");
            return true;
        } else {
            setStatus(State::Available, &manifest,
                "Update available \xe2\x80\x94 " + manifest.version,
                manifest.url.empty() ? manifest.notesUrl : manifest.url);
        }
        if (manifest.url.empty()) return true;
        if (!canReplaceInstall(executable)) {
            setStatus(State::ManualDownload, &manifest,
                "Update available; install location is not writable",
                manifest.notesUrl);
            return true;
        }
        setStatus(State::Downloading, &manifest, "Downloading update " + manifest.version);
        return downloadAndStagePublic(manifest, error);
    }

    bool downloadAndStagePublic(const Manifest& manifest, std::string& error) {
        const auto part = updateDir() / (artifactName(manifest.version) + ".part");
        if (!download(manifest, part, error)) {
            std::filesystem::remove(part);
            if (error.rfind("integrity: ", 0) == 0)
                reportOnce(manifest, error.substr(std::strlen("integrity: ")));
            setStatus(State::Error, &manifest, "Update download failed: " + error,
                      manifest.notesUrl);
            return false;
        }
        if (!verifyAndStage(manifest, part, error)) {
            setStatus(State::Error, &manifest, error, manifest.notesUrl);
            return false;
        }
        return true;
    }

    void loop() {
        auto backoff = std::chrono::duration_cast<std::chrono::seconds>(kInitialBackoff);
        while (true) {
            {
                std::lock_guard<std::mutex> lock(mutex);
                if (stopping) break;
                if (!mandatoryActive) {
                    status.state = State::Checking;
                    status.message = "Checking for updates";
                }
            }
            const bool ok = checkOnce();
            const auto delay = ok
                ? std::chrono::duration_cast<std::chrono::seconds>(kCheckInterval)
                : backoff;
            backoff = ok
                ? std::chrono::duration_cast<std::chrono::seconds>(kInitialBackoff)
                : std::min(backoff * 2,
                      std::chrono::duration_cast<std::chrono::seconds>(kMaxBackoff));
            std::unique_lock<std::mutex> lock(mutex);
            wake.wait_for(lock, delay, [&] { return stopping || forceCheck; });
            forceCheck = false;
            if (stopping) break;
        }
        std::lock_guard<std::mutex> lock(mutex);
        running = false;
    }
};

Updater::Updater() : impl_(std::make_unique<Impl>()) {}
Updater::~Updater() { stop(); }

void Updater::configure(std::string currentVersion, std::filesystem::path dataDir,
                        std::filesystem::path executablePath) {
    stop();
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->currentVersion = std::move(currentVersion);
    impl_->dataDir = std::move(dataDir);
    impl_->executablePath = std::move(executablePath);
    std::filesystem::create_directories(impl_->updateDir());
    impl_->loadReports();
    impl_->configured = true;
    impl_->stopping = false;
    impl_->mandatoryActive = false;
    impl_->status = {};
}

ApplyResult Updater::applyStagedBeforeWindow() {
    Manifest manifest;
    std::filesystem::path artifact;
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        if (!impl_->configured || !std::filesystem::is_regular_file(impl_->stagedMetadata()))
            return ApplyResult::NothingStaged;
        try {
            const json value = json::parse(std::ifstream(impl_->stagedMetadata()));
            manifest.version = value.at("version").get<std::string>();
            manifest.minSupported = value.at("min_supported").get<std::string>();
            manifest.sha256 = value.at("sha256").get<std::string>();
            manifest.size = value.at("size").get<std::uint64_t>();
            manifest.signature = value.at("signature").get<std::string>();
            manifest.notesUrl = value.at("notes_url").get<std::string>();
            manifest.url = value.value("url", std::string());
            artifact = impl_->updateDir() / value.at("artifact").get<std::string>();
        } catch (const std::exception&) {
            std::filesystem::remove(impl_->stagedMetadata());
            return ApplyResult::Rejected;
        }
        impl_->mandatoryActive =
            requiresMandatoryUpdate(impl_->currentVersion, manifest.minSupported);
    }
    std::string error;
    if (!impl_->verifyFile(manifest, artifact, error, true)) {
        std::filesystem::remove(impl_->stagedMetadata());
        return ApplyResult::Rejected;
    }
    const ApplyResult result = applyPlatformArtifact(
        artifact, impl_->executablePath, manifest.version, &error);
    if (result == ApplyResult::ManualDownload) {
        impl_->setStatus(State::ManualDownload, &manifest, error,
                         manifest.notesUrl);
    } else if (result == ApplyResult::Failed) {
        impl_->setStatus(State::Error, &manifest, error, manifest.notesUrl);
    }
    return result;
}

void Updater::start() {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if (!impl_->configured || impl_->running) return;
    impl_->stopping = false;
    impl_->running = true;
    impl_->worker = std::thread([this] { impl_->loop(); });
}

void Updater::stop() {
    {
        std::lock_guard<std::mutex> lock(impl_->mutex);
        impl_->stopping = true;
        impl_->wake.notify_all();
    }
    if (impl_->worker.joinable()) impl_->worker.join();
}

void Updater::setEntitlementToken(std::string token) {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if (impl_->token == token) return;
    impl_->token = std::move(token);
    impl_->forceCheck = true;
    impl_->wake.notify_all();
}

Snapshot Updater::snapshot() const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    return impl_->status;
}

bool Updater::downloadAndStage(const Manifest& manifest, std::string* error) {
    std::string detail;
    const bool ok = impl_->downloadAndStagePublic(manifest, detail);
    if (error) *error = detail;
    return ok;
}

bool Updater::verifyAndStageDownloaded(const Manifest& manifest,
                                       const std::filesystem::path& downloaded,
                                       std::string* error) {
    std::string detail;
    const bool ok = impl_->verifyAndStage(manifest, downloaded, detail);
    if (error) *error = detail;
    return ok;
}

void Updater::setTestSnapshot(Snapshot value) {
    stop();
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->status = std::move(value);
    impl_->mandatoryActive = value.mandatory;
}

Updater& globalUpdater() {
    static Updater value;
    return value;
}

}  // namespace chudware::update
