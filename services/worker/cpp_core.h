// CppCore — the host side of the strangler seam to the pure-C++ core.
//
// Loads libchudware.dll (the SEPARATE C++ core DLL) at
// RUNTIME via LoadLibrary and dispatches JSON-RPC-style calls into it through the
// flat C ABI in core/api/libchudware.h. The host build links NEITHER the core nor
// OCCT — the DLL and its OCCT runtime DLLs are loaded lazily on first use, so the
// host stays buildable/launchable even when the core isn't present.
//
// Today this drives the production doc/agent/sim routing seam (see worker_client.cpp) —
// unconditional: the C++ core is the only engine backend.
#pragma once

#include <atomic>
#include <mutex>
#include <string>
#include <cstdint>

#include <nlohmann/json.hpp>

namespace core {

class CppCore {
public:
    // Lazily load libchudware.{so,dylib,dll}. Developer builds retain explicit,
    // build-tree and loader-search candidates. Production loads only the canonical
    // installed payload location after authenticating its exact signed manifest row.
    // Logs the winner once to stderr: "[cpp_core] loaded libchudware ... path=...".
    // Safe to call repeatedly; only loads once.
    bool ensureLoaded(std::string& err);
    bool available() const { return loaded_.load(std::memory_order_acquire); }
    std::string abiVersion() const { return available() ? abi_ : std::string(); }
    // git-describe build id of the loaded engine ("" until loaded, or when the
    // .so predates the chudware_build_id export — callers tolerate absence).
    std::string buildId() const { return available() ? build_id_ : std::string(); }
    // Absolute path of the loaded library ("" until loaded). The hostd stale-
    // binary self-drain stats this file for mtime changes.
    std::string loadedPath() const { return available() ? path_ : std::string(); }

    // Dispatch a call into the C++ core. Returns true iff the core HANDLED the
    // method (chudware_call returned 0); `result` then holds its result object. On
    // false, `err` carries the reason (unknown method / load failure / throw) and
    // the caller should fall back to the Python worker. `result` still receives any
    // {"error":...} payload the core produced.
    bool call(const std::string& method, const nlohmann::json& params,
              nlohmann::json& result, std::string& err);

    // Live event streaming: register a THREAD-LOCAL callback the core invokes with each
    // simulation progress event's JSON (UTF-8) as it is emitted. Call with cb=nullptr to clear.
    // No-op if the loaded engine predates the chudware_set_event_sink export. Set on the SAME
    // thread that then calls call() for the streaming method.
    void setEventSink(void (*cb)(const char* event_json, void* ud), void* ud);
    void setRequestCancel(int (*check)(void*), void* user);
    // True iff the loaded engine exports chudware_set_event_sink (live streaming supported).
    // Callers use this to decide whether live events will fire (else fall back to batched).
    bool hasEventSink();

    // Optional process-wide publication from asynchronous persisted-cache verification.
    // Missing export means unsupported; the caller can retain the immediate warm snapshot.
    bool setCacheRefreshSink(
        void (*cb)(const char* doc_id, const char* response_json, void* ud), void* ud);

    // Dedicated optional lifecycle export. Missing/busy capability is unknown.
    bool activity(std::uint64_t counts[3]);

    // Teardown only: caller has stopped submitting engine work.
    bool drainBackgroundTasks();


private:
    std::mutex mtx_;
    bool tried_ = false;
    std::atomic<bool> loaded_{false};  // release-publishes immutable metadata and exports
    std::string abi_;
    std::string build_id_;
    std::string path_;
    void* dll_ = nullptr;  // HMODULE
    int (*call_)(const char*, const char*, char**) = nullptr;
    void (*free_)(char*) = nullptr;
    const char* (*abi_fn_)(void) = nullptr;
    void (*set_event_sink_)(void (*)(const char*, void*), void*) = nullptr;  // optional export
    void (*set_request_cancel_)(int (*)(void*), void*) = nullptr;
    void (*set_cache_refresh_sink_)(
        void (*)(const char*, const char*, void*), void*) = nullptr;
    int (*activity_)(std::uint64_t*) = nullptr;
    int (*drain_background_tasks_)() = nullptr;
};

// Process-wide singleton. The diagnostic seam is dev-only and gated behind an env
// flag, so a singleton keeps the host call sites surgical (no signature churn).
CppCore& cpp_core();

}  // namespace core
