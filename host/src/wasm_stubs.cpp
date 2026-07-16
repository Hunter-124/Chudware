// EMSCRIPTEN-only stubs for symbols / entry points that the full host links but
// this wave's slim WASM target does not yet port. Next wave un-stubs:
//   - services/ipc/* websocket transport (connectTo / spawnDetached / hostd)
//   - host/src/renderer.cpp + render/* GLSL ES shims (#version 330 -> 300 es)
//   - host/src/ui/file_dialog.cpp
//   - CEF / camera / V4L2 backends
//
// The slim wasm_boot.cpp path does not call these; they exist so a fuller
// EMSCRIPTEN source list can link without dragging in AF_UNIX / fork / glad.
#ifdef __EMSCRIPTEN__

#include <cstdio>
#include <string>

#include <nlohmann/json.hpp>

// ---- bootstrap (no python runtime / no /proc/self/exe on the web) ----------
// When the full app.cpp is re-enabled for WASM these soft-fail instead of
// pulling POSIX process + filesystem provisioning.
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

// ---- IPC transport stubs (real WS gateway is next wave) --------------------
namespace aicad_ipc {

struct IpcConnStub {
    bool valid() const { return false; }
    void close() {}
    bool eof() const { return true; }
};

// Soft-fail connect so any accidental call site reports a clear error.
inline bool wasmIpcUnsupported(std::string& err) {
    err = "WASM: unix-socket IPC not available (websocket gateway next wave)";
    std::fprintf(stderr, "[wasm_stubs] %s\n", err.c_str());
    return false;
}

// Named to match the real header APIs when those TUs are excluded from the
// EMSCRIPTEN target. If a future source starts calling them, link succeeds and
// runtime fails gracefully.
bool spawnDetached(const std::string& /*exe*/, const std::string& /*arg*/, std::string& err) {
    return wasmIpcUnsupported(err);
}

std::string socketPath(const std::string& /*key*/) { return ""; }
std::string runtimeDir() { return ""; }
std::string resolveHostdBin(const std::string& /*hint*/) { return ""; }
std::string instanceKey(const std::string& /*exePath*/) { return "wasm"; }

}  // namespace aicad_ipc

// ---- engine backend soft-fail ----------------------------------------------
namespace engine {
bool daemonMode() { return false; }
bool available() { return false; }
bool ensureLoaded(std::string& err) {
    err = "WASM: engine backend not linked this wave";
    return false;
}
bool call(const std::string& method, const nlohmann::json& /*params*/,
          nlohmann::json& /*result*/, std::string& err, int /*timeoutMs*/ = -1) {
    err = "WASM: engine call '" + method + "' not available (IPC next wave)";
    return false;
}
}  // namespace engine

#endif  // __EMSCRIPTEN__
