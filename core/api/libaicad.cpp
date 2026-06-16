// libaicad C ABI implementation — see libaicad.h.
//
// Dispatches string-in/string-out JSON calls to the C++ core. Today it serves:
//   ping             liveness + ABI echo (no kernel work; used by the host to
//                    confirm the DLL loaded and its OCCT deps resolved).
//   doc_compile_cpp  DIAGNOSTIC compile: takes a full document feature tree +
//                    env, runs aicad::doc::compile_document, writes the surviving
//                    bodies to an AICM file, and returns the per-feature statuses
//                    + body count + the AICM path. This is the strangler's
//                    parity probe (Step 2 of docs/CPP_MIGRATION.md): the host can
//                    feed the SAME tree it sends to the Python worker and diff the
//                    results, WITHOUT changing the production Python path.
//
// Everything here is exception-safe: any throw becomes a nonzero return with an
// {"error":...} payload so a bug in the C++ core can never crash the host.

#include "api/libaicad.h"

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/document.h"

using nlohmann::json;

namespace {

// malloc a NUL-terminated copy of `s` so the host frees it with aicad_free()
// (std::free). Returns nullptr only on allocation failure.
char* dup_cstr(const std::string& s) {
    char* p = static_cast<char*>(std::malloc(s.size() + 1));
    if (!p) return nullptr;
    std::memcpy(p, s.data(), s.size());
    p[s.size()] = '\0';
    return p;
}

// Emit `j` into *out_json (heap) and return `rc`. Always leaves *out_json valid.
int emit(json&& j, char** out_json, int rc) {
    if (out_json) *out_json = dup_cstr(j.dump());
    return rc;
}

int emit_error(const std::string& msg, char** out_json) {
    return emit(json{{"error", msg}}, out_json, 1);
}

// Choose where to write the AICM bytes. Honour an explicit params["aicm_out"]
// (the host/tests can pin a path), else a unique file in the OS temp dir. The
// counter + pid keep concurrent compiles from colliding without needing a clock
// (Date/now is intentionally avoided elsewhere in the codebase).
std::filesystem::path aicm_target(const json& params) {
    if (params.contains("aicm_out") && params["aicm_out"].is_string()) {
        return std::filesystem::path(params["aicm_out"].get<std::string>());
    }
    static std::atomic<unsigned long long> seq{0};
    unsigned long long n = seq.fetch_add(1);
    std::filesystem::path dir = std::filesystem::temp_directory_path();
    return dir / ("aicad_cpp_" + std::to_string(n) + ".aicm");
}

// doc_compile_cpp: compile a document tree to AICM + statuses (diagnostic).
int handle_compile(const json& params, char** out_json) {
    // The tree may arrive under "doc" (the {"features":[...]} shape compile_document
    // wants) or, for convenience, the host's tree may be passed directly. Accept
    // either: if there's a top-level "features", treat params itself as the doc.
    json doc;
    if (params.contains("doc")) {
        doc = params["doc"];
    } else if (params.contains("features")) {
        doc = params;
    } else {
        return emit_error("doc_compile_cpp: params need a 'doc' object (or a top-level 'features')", out_json);
    }
    json env = params.contains("env") ? params["env"] : json::object();

    aicad::doc::CompileResult res = aicad::doc::compile_document(doc, env);

    json statuses = json::array();
    for (const auto& s : res.statuses) {
        statuses.push_back(json{
            {"id", s.id},
            {"type", s.type},
            {"ok", s.ok},
            {"error", s.error},
        });
    }

    std::string aicm_path;
    if (!res.aicm.empty()) {
        std::filesystem::path target = aicm_target(params);
        std::ofstream f(target, std::ios::binary | std::ios::trunc);
        if (!f) {
            return emit_error("doc_compile_cpp: cannot open AICM output: " + target.string(), out_json);
        }
        f.write(reinterpret_cast<const char*>(res.aicm.data()),
                static_cast<std::streamsize>(res.aicm.size()));
        f.close();
        aicm_path = std::filesystem::absolute(target).string();
    }

    return emit(json{
                    {"ok", res.ok},
                    {"body_count", res.body_count},
                    {"volume", res.volume},
                    {"statuses", statuses},
                    {"aicm_path", aicm_path},
                    {"aicm_bytes", static_cast<uint64_t>(res.aicm.size())},
                },
                out_json, 0);
}

}  // namespace

extern "C" {

AICAD_API const char* aicad_abi_version(void) { return "1"; }

AICAD_API void aicad_free(char* s) { std::free(s); }

AICAD_API int aicad_call(const char* method, const char* params_json, char** out_json) {
    if (out_json) *out_json = nullptr;
    if (!method) return emit_error("null method", out_json);

    std::string m(method);
    json params = json::object();
    try {
        if (params_json && params_json[0] != '\0') {
            params = json::parse(params_json);
        }
    } catch (const std::exception& e) {
        return emit_error(std::string("invalid params JSON: ") + e.what(), out_json);
    }

    try {
        if (m == "ping") {
            return emit(json{{"ok", true}, {"abi", aicad_abi_version()}, {"echo", params}},
                        out_json, 0);
        }
        if (m == "doc_compile_cpp") {
            return handle_compile(params, out_json);
        }
        // Unknown to the C++ core -> nonzero so the host falls back to Python.
        return emit(json{{"error", "unknown method: " + m}}, out_json, 2);
    } catch (const std::exception& e) {
        return emit_error(std::string("aicad_call(") + m + ") threw: " + e.what(), out_json);
    } catch (...) {
        return emit_error("aicad_call(" + m + ") threw a non-std exception", out_json);
    }
}

}  // extern "C"
