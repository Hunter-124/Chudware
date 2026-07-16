// EMSCRIPTEN-only stubs for symbols that have no browser implementation.
// Real IPC lives in services/ipc/ipc_transport_wasm.cpp + ipc_client.cpp.
// Real engine::* lives in services/worker/engine_backend.cpp.
// Bootstrap symbols (exeDir/runtime/…) are stubbed here because bootstrap.cpp
// is excluded from the EMSCRIPTEN source list.
#ifdef __EMSCRIPTEN__

#include "ipc_discovery.h"

#include <cstdint>
#include <cstdio>
#include <string>

// ---- bootstrap (no python runtime / no /proc/self/exe on the web) ----------
std::string exeDir() { return "/"; }
std::string runtimePython() { return ""; }
std::string appDir() { return "/"; }
bool runtimeReady() { return false; }
bool ensureRuntime(std::string& log) {
    log = "WASM: runtime provisioning not available";
    return false;
}
bool repairRuntime(std::string& log) {
    log = "WASM: runtime repair not available";
    return false;
}
void ensurePromptsDir() {}

// ---- IPC discovery / spawn (browser never forks hostd) --------------------
namespace aicad_ipc {

bool spawnDetached(const std::string& /*exe*/, const std::string& /*arg*/, std::string& err) {
    err = "WASM: spawnDetached not available (gateway-hosted daemon)";
    return false;
}

// socketPath is ignored by connectTo under EMSCRIPTEN (dials location.host/engine)
// but EngineClient still needs a non-empty path to pass its empty-check.
std::string socketPath(const std::string& /*key*/) { return "ws://engine"; }

std::string runtimeDir() { return "/tmp"; }

std::string resolveHostdBin(const std::string& /*hint*/) { return ""; }

std::string instanceKey(const std::string& /*exePath*/) { return "wasm"; }

std::string lockPath(const std::string& key) { return "/tmp/aicad-lock-" + key; }

std::string stateDir() { return "/tmp"; }

std::string fnv1aHex(const std::string& s) {
    uint64_t h = 14695981039346656037ull;
    for (unsigned char c : s) {
        h ^= c;
        h *= 1099511628211ull;
    }
    char buf[17];
    for (int i = 15; i >= 0; --i) {
        const int n = (int)(h & 0xF);
        buf[i] = (char)(n < 10 ? '0' + n : 'a' + (n - 10));
        h >>= 4;
    }
    buf[16] = 0;
    return std::string(buf);
}

// SingleInstance is daemon-side only; provide no-op bodies so the class links.
bool SingleInstance::acquire(const std::string& /*lockFile*/, std::string& err) {
    err = "WASM: SingleInstance not available";
    return false;
}
void SingleInstance::release() {}

}  // namespace aicad_ipc

#endif  // __EMSCRIPTEN__
