#pragma once

#include "security/integrity.h"

#include <filesystem>
#include <functional>
#include <string>
#include <string_view>
#include <cstdint>
#include <vector>

namespace chudware::security {

enum class SecurityCheckpoint {
    Startup,
    BeforeMutation,
    BeforeAi,
    BeforeWorkerSpawn,
};

inline constexpr std::string_view kReasonProtectedFileHashMismatch =
    "protected_file_hash_mismatch";
inline constexpr std::string_view kReasonProtectedModuleUnlisted =
    "protected_module_unlisted";
inline constexpr std::string_view kReasonDebuggerAttached = "debugger_attached";
inline constexpr std::string_view kReasonTraceEnvironmentPresent =
    "trace_environment_present";

struct LoadedModule {
    std::filesystem::path path;
    bool chudwareOwned = false;
};

struct RuntimeGuardProbes {
    std::function<bool()> debuggerAttached;
    std::function<bool()> traceEnvironmentPresent;
    std::function<std::vector<LoadedModule>()> loadedModules;
};

struct SecuritySnapshot {
    SecurityCheckpoint checkpoint = SecurityCheckpoint::Startup;
    std::vector<std::string> reasonCodes;
    int vmContainerScore = 0;

    bool ok() const noexcept { return reasonCodes.empty(); }
};

struct RuntimeGuardDecision {
    SecuritySnapshot snapshot;
    std::string denialReason;

    bool allowed() const noexcept { return snapshot.ok(); }
};

RuntimeGuardProbes platformRuntimeGuardProbes();

SecuritySnapshot inspectRuntime(
    SecurityCheckpoint checkpoint,
    const std::filesystem::path& payloadRoot,
    const std::vector<FileVerdict>& verifiedManifestFiles,
    RuntimeGuardProbes probes = platformRuntimeGuardProbes());

// Inspect a protected-operation checkpoint and durably queue every approved,
// nonzero reason before returning a denial. Queue failure remains a denial and
// is exposed only as the bounded, non-sensitive persistence reason below.
RuntimeGuardDecision inspectAndPersistRuntime(
    SecurityCheckpoint checkpoint,
    const std::filesystem::path& payloadRoot,
    const std::vector<FileVerdict>& verifiedManifestFiles,
    std::string_view stateDir,
    RuntimeGuardProbes probes = platformRuntimeGuardProbes(),
    std::int64_t occurredAt = 0);

inline constexpr std::string_view kRuntimeEvidencePersistenceFailed =
    "security_evidence_persistence_failed";

// Canonical per-user state directory shared by GUI, hostd, agents, and workers.
std::filesystem::path runtimeGuardStateDir();

}  // namespace chudware::security
