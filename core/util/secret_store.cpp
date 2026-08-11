#include "util/secret_store.h"

#include <array>
#include <atomic>
#include <cerrno>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <system_error>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <dpapi.h>
#elif defined(__APPLE__)
#  include <Security/Security.h>
#else
#  include <fcntl.h>
#  include <sys/stat.h>
#  include <sys/types.h>
#  include <sys/wait.h>
#  include <unistd.h>
#endif

namespace chudware::secret_store {
namespace {

namespace fs = std::filesystem;
std::atomic<bool> warnedFallback{false};

std::string safeName(std::string_view service, std::string_view account) {
    std::string out;
    for (unsigned char c : std::string(service) + "-" + std::string(account))
        out.push_back((std::isalnum(c) || c == '-' || c == '_') ? static_cast<char>(c) : '_');
    return out.empty() ? "secret" : out;
}

fs::path fallbackPath(std::string_view service, std::string_view account,
                      std::string_view dataDir, const char* suffix = ".secret") {
    return fs::path(dataDir) / "secrets" / (safeName(service, account) + suffix);
}

void warnOnce(const fs::path& path) {
    bool expected = false;
    if (warnedFallback.compare_exchange_strong(expected, true)) {
        std::fprintf(stderr,
                     "[chudware] WARNING: no OS keychain is available; entitlement data is "
                     "stored in the 0600 fallback file %s\n",
                     path.string().c_str());
        std::fflush(stderr);
    }
}

bool atomicWrite0600(const fs::path& path, const std::vector<unsigned char>& value) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    if (ec) return false;
    const fs::path tmp = path.string() + ".tmp";
#if !defined(_WIN32)
    const int fd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) return false;
    std::size_t done = 0;
    while (done < value.size()) {
        const ssize_t n = ::write(fd, value.data() + done, value.size() - done);
        if (n > 0) done += static_cast<std::size_t>(n);
        else if (n < 0 && errno == EINTR) continue;
        else {
            ::close(fd);
            ::unlink(tmp.c_str());
            return false;
        }
    }
    const bool synced = ::fsync(fd) == 0;
    ::close(fd);
    if (!synced) {
        ::unlink(tmp.c_str());
        return false;
    }
#else
    std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
    if (!out) return false;
    out.write(reinterpret_cast<const char*>(value.data()), static_cast<std::streamsize>(value.size()));
    out.close();
    if (!out) return false;
#endif
    fs::rename(tmp, path, ec);
    if (ec) {
        fs::remove(path, ec);
        ec.clear();
        fs::rename(tmp, path, ec);
    }
#if !defined(_WIN32)
    if (!ec) ::chmod(path.c_str(), 0600);
#endif
    return !ec;
}

bool readFile(const fs::path& path, std::vector<unsigned char>& value) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    in.seekg(0, std::ios::end);
    const auto length = in.tellg();
    if (length < 0 || length > 1024 * 1024) return false;
    in.seekg(0);
    value.resize(static_cast<std::size_t>(length));
    in.read(reinterpret_cast<char*>(value.data()), length);
    return static_cast<bool>(in) || (length == 0 && in.eof());
}

std::string base64(const std::vector<unsigned char>& data) {
    static constexpr char chars[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve((data.size() + 2) / 3 * 4);
    for (std::size_t i = 0; i < data.size(); i += 3) {
        unsigned int v = static_cast<unsigned int>(data[i]) << 16;
        if (i + 1 < data.size()) v |= static_cast<unsigned int>(data[i + 1]) << 8;
        if (i + 2 < data.size()) v |= data[i + 2];
        out.push_back(chars[(v >> 18) & 63]);
        out.push_back(chars[(v >> 12) & 63]);
        out.push_back(i + 1 < data.size() ? chars[(v >> 6) & 63] : '=');
        out.push_back(i + 2 < data.size() ? chars[v & 63] : '=');
    }
    return out;
}

bool unbase64(std::string_view input, std::vector<unsigned char>& out) {
    auto value = [](unsigned char c) -> int {
        if (c >= 'A' && c <= 'Z') return c - 'A';
        if (c >= 'a' && c <= 'z') return c - 'a' + 26;
        if (c >= '0' && c <= '9') return c - '0' + 52;
        if (c == '+') return 62;
        if (c == '/') return 63;
        return -1;
    };
    while (!input.empty() && (input.back() == '\n' || input.back() == '\r')) input.remove_suffix(1);
    if (input.empty() || input.size() % 4) return false;
    out.clear();
    for (std::size_t i = 0; i < input.size(); i += 4) {
        int a = value(input[i]);
        int b = value(input[i + 1]);
        int c = input[i + 2] == '=' ? 0 : value(input[i + 2]);
        int d = input[i + 3] == '=' ? 0 : value(input[i + 3]);
        if (a < 0 || b < 0 || c < 0 || d < 0) return false;
        const unsigned int v = (a << 18) | (b << 12) | (c << 6) | d;
        out.push_back(static_cast<unsigned char>(v >> 16));
        if (input[i + 2] != '=') out.push_back(static_cast<unsigned char>(v >> 8));
        if (input[i + 3] != '=') out.push_back(static_cast<unsigned char>(v));
    }
    return true;
}

#if !defined(_WIN32) && !defined(__APPLE__)
int runSecretTool(const std::vector<std::string>& args, std::string_view input,
                  std::string* output) {
    int inputPipe[2] = {-1, -1};
    int outputPipe[2] = {-1, -1};
    if (::pipe(inputPipe) != 0 || (output && ::pipe(outputPipe) != 0)) return -1;
    const pid_t child = ::fork();
    if (child < 0) return -1;
    if (child == 0) {
        ::dup2(inputPipe[0], STDIN_FILENO);
        ::close(inputPipe[0]);
        ::close(inputPipe[1]);
        if (output) {
            ::dup2(outputPipe[1], STDOUT_FILENO);
            ::close(outputPipe[0]);
            ::close(outputPipe[1]);
        }
        std::vector<char*> argv;
        argv.reserve(args.size() + 2);
        argv.push_back(const_cast<char*>("secret-tool"));
        for (const std::string& arg : args) argv.push_back(const_cast<char*>(arg.c_str()));
        argv.push_back(nullptr);
        ::execvp("secret-tool", argv.data());
        _exit(127);
    }
    ::close(inputPipe[0]);
    if (output) ::close(outputPipe[1]);
    std::size_t done = 0;
    while (done < input.size()) {
        const ssize_t n = ::write(inputPipe[1], input.data() + done, input.size() - done);
        if (n > 0) done += static_cast<std::size_t>(n);
        else if (n < 0 && errno == EINTR) continue;
        else break;
    }
    ::close(inputPipe[1]);
    if (output) {
        output->clear();
        std::array<char, 4096> buffer{};
        for (;;) {
            const ssize_t n = ::read(outputPipe[0], buffer.data(), buffer.size());
            if (n > 0) output->append(buffer.data(), static_cast<std::size_t>(n));
            else if (n < 0 && errno == EINTR) continue;
            else break;
        }
        ::close(outputPipe[0]);
    }
    int status = 0;
    while (::waitpid(child, &status, 0) < 0 && errno == EINTR) {}
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}
#endif

}  // namespace

bool store(std::string_view service, std::string_view account,
           const std::vector<unsigned char>& value, std::string_view dataDir) {
#if defined(_WIN32)
    DATA_BLOB plain{static_cast<DWORD>(value.size()), const_cast<BYTE*>(value.data())};
    DATA_BLOB encrypted{};
    if (!CryptProtectData(&plain, L"Chudware entitlement", nullptr, nullptr, nullptr,
                          CRYPTPROTECT_UI_FORBIDDEN, &encrypted))
        return false;
    std::vector<unsigned char> bytes(encrypted.pbData, encrypted.pbData + encrypted.cbData);
    LocalFree(encrypted.pbData);
    return atomicWrite0600(fallbackPath(service, account, dataDir, ".dpapi"), bytes);
#elif defined(__APPLE__)
    UInt32 oldLength = 0;
    void* oldData = nullptr;
    SecKeychainItemRef item = nullptr;
    const OSStatus found = SecKeychainFindGenericPassword(
        nullptr, static_cast<UInt32>(service.size()), service.data(),
        static_cast<UInt32>(account.size()), account.data(), &oldLength, &oldData, &item);
    if (oldData) SecKeychainItemFreeContent(nullptr, oldData);
    OSStatus status = errSecSuccess;
    if (found == errSecSuccess) {
        status = SecKeychainItemModifyAttributesAndData(
            item, nullptr, static_cast<UInt32>(value.size()), value.data());
        CFRelease(item);
    } else if (found == errSecItemNotFound) {
        status = SecKeychainAddGenericPassword(
            nullptr, static_cast<UInt32>(service.size()), service.data(),
            static_cast<UInt32>(account.size()), account.data(),
            static_cast<UInt32>(value.size()), value.data(), nullptr);
    } else {
        status = found;
    }
    if (status == errSecSuccess) return true;
#else
    const std::string encoded = base64(value);
    if (runSecretTool({"store", "--label=Chudware entitlement", "service",
                       std::string(service), "account", std::string(account)},
                      encoded, nullptr) == 0)
        return true;
#endif
    const fs::path path = fallbackPath(service, account, dataDir);
    warnOnce(path);
    return atomicWrite0600(path, value);
}

bool load(std::string_view service, std::string_view account,
          std::vector<unsigned char>& value, std::string_view dataDir) {
#if defined(_WIN32)
    std::vector<unsigned char> encrypted;
    if (!readFile(fallbackPath(service, account, dataDir, ".dpapi"), encrypted)) return false;
    DATA_BLOB source{static_cast<DWORD>(encrypted.size()), encrypted.data()};
    DATA_BLOB plain{};
    if (!CryptUnprotectData(&source, nullptr, nullptr, nullptr, nullptr,
                            CRYPTPROTECT_UI_FORBIDDEN, &plain))
        return false;
    value.assign(plain.pbData, plain.pbData + plain.cbData);
    LocalFree(plain.pbData);
    return true;
#elif defined(__APPLE__)
    UInt32 length = 0;
    void* data = nullptr;
    SecKeychainItemRef item = nullptr;
    const OSStatus status = SecKeychainFindGenericPassword(
        nullptr, static_cast<UInt32>(service.size()), service.data(),
        static_cast<UInt32>(account.size()), account.data(), &length, &data, &item);
    if (status == errSecSuccess) value.assign(static_cast<unsigned char*>(data),
                                             static_cast<unsigned char*>(data) + length);
    if (data) SecKeychainItemFreeContent(nullptr, data);
    if (item) CFRelease(item);
    if (status == errSecSuccess) return true;
#else
    std::string encoded;
    if (runSecretTool({"lookup", "service", std::string(service), "account",
                       std::string(account)}, {}, &encoded) == 0 &&
        unbase64(encoded, value))
        return true;
#endif
    const fs::path path = fallbackPath(service, account, dataDir);
    if (!fs::exists(path)) return false;
    warnOnce(path);
    return readFile(path, value);
}

bool erase(std::string_view service, std::string_view account, std::string_view dataDir) {
#if defined(_WIN32)
    std::error_code ec;
    fs::remove(fallbackPath(service, account, dataDir, ".dpapi"), ec);
    return !ec;
#elif defined(__APPLE__)
    UInt32 length = 0;
    void* data = nullptr;
    SecKeychainItemRef item = nullptr;
    const OSStatus found = SecKeychainFindGenericPassword(
        nullptr, static_cast<UInt32>(service.size()), service.data(),
        static_cast<UInt32>(account.size()), account.data(), &length, &data, &item);
    if (data) SecKeychainItemFreeContent(nullptr, data);
    if (found == errSecItemNotFound) return true;
    const OSStatus status = found == errSecSuccess ? SecKeychainItemDelete(item) : found;
    if (item) CFRelease(item);
    if (status == errSecSuccess) return true;
#else
    if (runSecretTool({"clear", "service", std::string(service), "account",
                       std::string(account)}, {}, nullptr) == 0)
        return true;
#endif
    std::error_code ec;
    fs::remove(fallbackPath(service, account, dataDir), ec);
    return !ec;
}

}  // namespace chudware::secret_store
