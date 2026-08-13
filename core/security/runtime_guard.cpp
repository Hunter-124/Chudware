#include "security/runtime_guard.h"
#include "security/event_queue.h"

#include <algorithm>
#include <array>
#include <cstdlib>
#include <fstream>
#include <set>
#include <string>
#include <chrono>
#include <system_error>

#if defined(_WIN32)
#define NOMINMAX
#include <windows.h>
#include <tlhelp32.h>
#elif defined(__APPLE__)
#include <mach-o/dyld.h>
#include <sys/sysctl.h>
#include <unistd.h>
#elif defined(__linux__)
#include <link.h>
#include <unistd.h>
#endif

namespace chudware::security {
namespace fs = std::filesystem;
namespace {

bool isTraceEnvironmentPresent() {
    static constexpr std::array<std::string_view, 8> names = {
        "LD_PRELOAD", "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH", "COR_ENABLE_PROFILING", "CORECLR_ENABLE_PROFILING",
        "PYTHONINSPECT"};
    for (const auto name : names) {
        const std::string key(name);
        if (const char* value = std::getenv(key.c_str()); value && *value) return true;
    }
    return false;
}

bool isDebuggerAttached() {
#if defined(_WIN32)
    if (IsDebuggerPresent()) return true;
    BOOL remote = FALSE;
    return CheckRemoteDebuggerPresent(GetCurrentProcess(), &remote) && remote != FALSE;
#elif defined(__APPLE__)
    int mib[4] = {CTL_KERN, KERN_PROC, KERN_PROC_PID, getpid()};
    kinfo_proc info{};
    std::size_t size = sizeof(info);
    return sysctl(mib, 4, &info, &size, nullptr, 0) == 0 &&
           (info.kp_proc.p_flag & P_TRACED) != 0;
#elif defined(__linux__)
    std::ifstream status("/proc/self/status");
    std::string line;
    while (std::getline(status, line)) {
        if (line.rfind("TracerPid:", 0) != 0) continue;
        const auto first = line.find_first_of("0123456789");
        return first != std::string::npos && line[first] != '0';
    }
    return false;
#else
    return false;
#endif
}

std::vector<LoadedModule> loadedModules() {
    std::vector<LoadedModule> out;
#if defined(_WIN32)
    const DWORD pid = GetCurrentProcessId();
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    if (snapshot == INVALID_HANDLE_VALUE) return out;
    MODULEENTRY32W entry{};
    entry.dwSize = sizeof(entry);
    if (Module32FirstW(snapshot, &entry)) {
        do {
            out.push_back({fs::path(entry.szExePath), true});
        } while (Module32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
#elif defined(__APPLE__)
    const auto count = _dyld_image_count();
    for (std::uint32_t i = 0; i < count; ++i) {
        if (const char* name = _dyld_get_image_name(i); name && *name)
            out.push_back({fs::path(name), true});
    }
#elif defined(__linux__)
    struct Context { std::vector<LoadedModule>* out; } context{&out};
    dl_iterate_phdr(
        [](dl_phdr_info* info, std::size_t, void* opaque) {
            if (info->dlpi_name && *info->dlpi_name)
                static_cast<Context*>(opaque)->out->push_back({fs::path(info->dlpi_name), true});
            return 0;
        },
        &context);
#endif
    return out;
}

fs::path normalized(const fs::path& path) {
    std::error_code ec;
    const auto result = fs::weakly_canonical(path, ec);
    return ec ? path.lexically_normal() : result;
}

bool within(const fs::path& child, const fs::path& root) {
    const auto c = normalized(child);
    const auto r = normalized(root);
    auto ci = c.begin();
    for (auto ri = r.begin(); ri != r.end(); ++ri, ++ci)
        if (ci == c.end() || *ci != *ri) return false;
    return true;
}

bool chudwareModule(const LoadedModule& module, const fs::path& root) {
    if (!module.chudwareOwned) return false;
    const auto name = module.path.filename().string();
    return within(module.path, root) &&
           (name.find("chudware") != std::string::npos || name.find("Chudware") != std::string::npos);
}

void addReason(SecuritySnapshot& snapshot, std::string_view reason) {
    const std::string value(reason);
    if (std::find(snapshot.reasonCodes.begin(), snapshot.reasonCodes.end(), value) ==
        snapshot.reasonCodes.end())
        snapshot.reasonCodes.push_back(value);
}

}  // namespace

RuntimeGuardProbes platformRuntimeGuardProbes() {
    return {isDebuggerAttached, isTraceEnvironmentPresent, loadedModules};
}

fs::path runtimeGuardStateDir() {
#if defined(_WIN32)
    if (const char* appdata = std::getenv("APPDATA"); appdata && *appdata)
        return fs::path(appdata) / "Chudware";
    return fs::path("Chudware");
#else
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && *xdg)
        return fs::path(xdg) / "Chudware";
    if (const char* home = std::getenv("HOME"); home && *home)
        return fs::path(home) / ".local" / "share" / "Chudware";
    return fs::path(".") / "Chudware";
#endif
}

SecuritySnapshot inspectRuntime(SecurityCheckpoint checkpoint,
                                const fs::path& payloadRoot,
                                const std::vector<FileVerdict>& verifiedManifestFiles,
                                RuntimeGuardProbes probes) {
    SecuritySnapshot snapshot;
    snapshot.checkpoint = checkpoint;
    snapshot.vmContainerScore = 0;
#ifdef CHUDWARE_DEVELOPER_BUILD
    (void)payloadRoot;
    (void)verifiedManifestFiles;
    (void)probes;
    return snapshot;
#endif

    if (probes.debuggerAttached && probes.debuggerAttached())
        addReason(snapshot, kReasonDebuggerAttached);
    if (probes.traceEnvironmentPresent && probes.traceEnvironmentPresent())
        addReason(snapshot, kReasonTraceEnvironmentPresent);

    std::set<fs::path> listed;
    for (const auto& file : verifiedManifestFiles) {
        const auto absolute = normalized(payloadRoot / fs::path(file.path));
        listed.insert(absolute);
        if (!file.ok && (file.reason == IntegrityReason::HashMismatch ||
                         file.reason == IntegrityReason::SizeMismatch ||
                         file.reason == IntegrityReason::ModeMismatch))
            addReason(snapshot, kReasonProtectedFileHashMismatch);
    }

    if (probes.loadedModules) {
        for (const auto& module : probes.loadedModules()) {
            if (!chudwareModule(module, payloadRoot)) continue;
            const auto path = normalized(module.path);
            if (listed.find(path) == listed.end()) {
                addReason(snapshot, kReasonProtectedModuleUnlisted);
                continue;
            }
            const auto it = std::find_if(verifiedManifestFiles.begin(), verifiedManifestFiles.end(),
                                         [&](const FileVerdict& file) {
                                             return normalized(payloadRoot / fs::path(file.path)) == path;
                                         });
            if (it != verifiedManifestFiles.end() && !it->ok)
                addReason(snapshot, kReasonProtectedFileHashMismatch);
        }
    }
    return snapshot;
}

RuntimeGuardDecision inspectAndPersistRuntime(
    SecurityCheckpoint checkpoint,
    const fs::path& payloadRoot,
    const std::vector<FileVerdict>& verifiedManifestFiles,
    std::string_view stateDir,
    RuntimeGuardProbes probes,
    std::int64_t occurredAt) {
    RuntimeGuardDecision decision;
    decision.snapshot = inspectRuntime(checkpoint, payloadRoot, verifiedManifestFiles,
                                       std::move(probes));
    if (decision.snapshot.ok()) return decision;

    if (occurredAt <= 0) {
        occurredAt = std::chrono::duration_cast<std::chrono::seconds>(
                         std::chrono::system_clock::now().time_since_epoch())
                         .count();
    }
    SecurityReporter reporter{std::string(stateDir)};
    for (const auto& reason : decision.snapshot.reasonCodes) {
        if (!approvedSecurityReason(reason) || securityEventScore(reason) == 0) continue;
        if (!reporter.report(reason, occurredAt)) {
            decision.denialReason = kRuntimeEvidencePersistenceFailed;
            return decision;
        }
    }
    decision.denialReason = decision.snapshot.reasonCodes.empty()
                                ? std::string(kReasonProtectedFileHashMismatch)
                                : decision.snapshot.reasonCodes.front();
    return decision;
}

}  // namespace chudware::security
