#pragma once

#include <cstdlib>
#include <filesystem>
#include <mutex>
#include <stdexcept>
#include <string>

namespace chudware::doc::native_policy {
// JSON/RPC paths are UTF-8; filesystem paths use the platform's native encoding.
// Keep the returned byte strings identical for C++17 core and C++20 host callers.
inline std::string path_utf8(const std::filesystem::path& path) {
#if defined(__cpp_char8_t)
    const auto text = path.u8string();
    return std::string(reinterpret_cast<const char*>(text.data()), text.size());
#else
    return path.u8string();
#endif
}
inline std::string generic_path_utf8(const std::filesystem::path& path) {
#if defined(__cpp_char8_t)
    const auto text = path.generic_u8string();
    return std::string(reinterpret_cast<const char*>(text.data()), text.size());
#else
    return path.generic_u8string();
#endif
}

inline thread_local bool recomputing_cache = false;
struct CacheRecomputeScope {
    bool previous = recomputing_cache;
    CacheRecomputeScope() { recomputing_cache = true; }
    ~CacheRecomputeScope() { recomputing_cache = previous; }
};
struct Configuration { std::mutex mutex; std::string root; std::string state; bool configured = false; };
inline Configuration& configuration() { static Configuration value; return value; }
inline void configure(const std::string& root, const std::string& state) {
    auto& value = configuration();
    std::lock_guard<std::mutex> lock(value.mutex);
    value.root = root; value.state = state; value.configured = true;
}
inline std::string setting(bool state) {
    auto& value = configuration();
    std::lock_guard<std::mutex> lock(value.mutex);
    if (value.configured) return state ? value.state : value.root;
    const char* env = std::getenv(state ? "CHUDWARE_CELL_STATE" : "CHUDWARE_NATIVE_ROOT");
    return env ? env : "";
}
inline bool rooted() { return !setting(false).empty(); }
inline std::string resolve_reference(const std::string& reference, const std::string& base = "") {
    namespace fs = std::filesystem;
    if (reference.find('\0') != std::string::npos)
        throw std::invalid_argument("native reference contains NUL");
    const std::string root = setting(false);
    if (root.empty()) return reference;
    const auto relative = fs::u8path(reference);
    if (reference.empty() || relative.is_absolute() || relative.has_root_name() ||
        reference.find('\\') != std::string::npos || reference.find(':') != std::string::npos)
        throw std::invalid_argument("native reference must be workspace-relative");
    for (const auto& component : relative)
        if (component == "..") throw std::invalid_argument("native reference traversal refused");
    const auto allowed = fs::weakly_canonical(fs::u8path(root));
    const auto candidate = (base.empty() ? allowed : fs::u8path(base)) / relative;
    // Inspect every existing component without following it. weakly_canonical alone
    // treats a dangling symlink as a missing lexical suffix and would admit it.
    fs::path walked;
    for (const auto& component : candidate) {
        walked /= component;
        std::error_code error;
        const auto status = fs::symlink_status(walked, error);
        if (fs::is_symlink(status))
            throw std::invalid_argument("native reference symlink traversal refused");
        if (error && error != std::errc::no_such_file_or_directory &&
            error != std::errc::not_a_directory)
            throw std::invalid_argument("native reference cannot be inspected");
    }
    const auto resolved = fs::weakly_canonical(candidate);
    auto r = resolved.begin();
    for (auto a = allowed.begin(); a != allowed.end(); ++a, ++r)
        if (r == resolved.end() || *r != *a)
            throw std::invalid_argument("native reference escapes allowed root");
    return path_utf8(resolved);
}
// Native RPCs may already map a user-relative path to an OS path. Persist only its
// rooted relative spelling; document-controlled references still use the strict
// resolve_reference entrypoint above and never gain absolute-path acceptance.
inline std::string authored_reference(const std::string& path) {
    namespace fs = std::filesystem;
    if (!rooted()) return resolve_reference(path);
    if (path.find('\0') != std::string::npos)
        throw std::invalid_argument("native reference contains NUL");
    const auto root = fs::weakly_canonical(fs::u8path(setting(false)));
    const auto native_path = fs::u8path(path);
    const auto relative = generic_path_utf8(
        (native_path.is_absolute() ? native_path : root / native_path).lexically_relative(root));
    resolve_reference(relative);
    return relative;
}
}  // namespace chudware::doc::native_policy
