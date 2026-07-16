// CppCore — the host side of the strangler seam to the pure-C++ core.
//
// Loads libaicad.dll (the SEPARATE C++ core DLL; see docs/CPP_MIGRATION.md) at
// RUNTIME via LoadLibrary and dispatches JSON-RPC-style calls into it through the
// flat C ABI in core/api/libaicad.h. The host build links NEITHER the core nor
// OCCT — the DLL and its OCCT runtime DLLs are loaded lazily on first use, so the
// host stays buildable/launchable even when the core isn't present.
//
// Today this drives the production doc/agent/sim routing seam (see worker_client.cpp) and the
// opt-in parity diagnostic (doc_compile_cpp when $AI_CAD_CPP_PARITY is set). As features reach
// parity the host routes their methods here and falls back to Python only where needed.
#pragma once

#include <mutex>
#include <string>

#include <nlohmann/json.hpp>

namespace core {

class CppCore {
public:
    // Lazily load libaicad.{so,dll}. Resolve order (first hit wins):
    //   1. $BILLET_LIBAICAD / $AI_CAD_LIBAICAD (explicit absolute path)
    //   2. AICAD_DEFAULT_LIBAICAD (compile-time build-tree path, if set)
    //   3. libaicad next to the running host/hostd executable (staged install)
    //   4. bare name via the dynamic loader (rpath / LD_DIRECTORY / PATH)
    // Logs the winner once to stderr: "[cpp_core] loaded libaicad ... path=...".
    // Safe to call repeatedly; only loads once.
    bool ensureLoaded(std::string& err);
    bool available() const { return loaded_; }
    std::string abiVersion() const { return abi_; }
    // git-describe build id of the loaded engine ("" until loaded, or when the
    // .so predates the aicad_build_id export — callers tolerate absence).
    std::string buildId() const { return build_id_; }
    // Absolute path of the loaded library ("" until loaded). The hostd stale-
    // binary self-drain stats this file for mtime changes.
    std::string loadedPath() const { return path_; }

    // Dispatch a call into the C++ core. Returns true iff the core HANDLED the
    // method (aicad_call returned 0); `result` then holds its result object. On
    // false, `err` carries the reason (unknown method / load failure / throw) and
    // the caller should fall back to the Python worker. `result` still receives any
    // {"error":...} payload the core produced.
    bool call(const std::string& method, const nlohmann::json& params,
              nlohmann::json& result, std::string& err);

private:
    std::mutex mtx_;
    bool tried_ = false;
    bool loaded_ = false;
    std::string abi_;
    std::string build_id_;
    std::string path_;
    void* dll_ = nullptr;  // HMODULE
    int (*call_)(const char*, const char*, char**) = nullptr;
    void (*free_)(char*) = nullptr;
    const char* (*abi_fn_)(void) = nullptr;
};

// Process-wide singleton. The diagnostic seam is dev-only and gated behind an env
// flag, so a singleton keeps the host call sites surgical (no signature churn).
CppCore& cpp_core();

}  // namespace core
