// chudware_probe — a standalone exe that loads libchudware.dll at RUNTIME (LoadLibrary
// + GetProcAddress), exactly the way the host (host/src/core/cpp_core.cpp) will.
//
// This deliberately does NOT link the core static libs or OCCT. Its whole purpose
// is to de-risk the boundary in isolation: prove that libchudware.dll loads, that its
// OCCT dependency DLLs (TK*, jemalloc, tbb12) resolve from the exe directory, that
// the C ABI is callable across a pure-C boundary, and that a real
// solve->extrude->tessellate compile runs through the DLL and produces AICM bytes.
// If this passes, the same DLL will load from the host with the same staging.
//
// Exit code 0 = all checks passed (ctest green); nonzero = a specific failure.

#include <cstdio>
#include <cstdlib>
#include <string>

#include <nlohmann/json.hpp>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <dlfcn.h>
#  include <unistd.h>
#endif

using nlohmann::json;

namespace {

using chudware_call_fn = int (*)(const char*, const char*, char**);
using chudware_free_fn = void (*)(char*);
using chudware_abi_fn = const char* (*)(void);

chudware_call_fn g_call = nullptr;
chudware_free_fn g_free = nullptr;
chudware_abi_fn g_abi = nullptr;

int fail(const char* msg) {
    std::fprintf(stderr, "[probe] FAIL: %s\n", msg);
    return 1;
}

// A minimal box document: rectangle (30 x 20) on the top plane -> extrude depth 25.
// Mirrors core/tests/test_document.cpp case 1 so we exercise the full
// solve_sketch -> faces_from_solved_sketch -> extrude -> tessellate path.
json box_doc() {
    auto line = [](const char* id, double x1, double y1, double x2, double y2) {
        return json{{"id", id}, {"type", "line"}, {"construction", false},
                    {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
    };
    auto rel = [](const char* id, const char* type, std::vector<const char*> refs) {
        json r = json::array();
        for (auto s : refs) r.push_back(s);
        return json{{"id", id}, {"type", type}, {"refs", r}};
    };
    auto dim = [](const char* id, std::vector<const char*> refs, double value) {
        json r = json::array();
        for (auto s : refs) r.push_back(s);
        return json{{"id", id}, {"type", "distance"}, {"refs", r},
                    {"value", json{{"value", value}, {"expr", nullptr}}}, {"driven", false}};
    };
    const double W = 30.0, H = 20.0;
    json entities = json::array({
        line("e1", 0, 0, W, 0),
        line("e2", W, 0, W, H),
        line("e3", W, H, 0, H),
        line("e4", 0, H, 0, 0),
    });
    json constraints = json::array({
        rel("c1", "coincident", {"e1.p2", "e2.p1"}),
        rel("c2", "coincident", {"e2.p2", "e3.p1"}),
        rel("c3", "coincident", {"e3.p2", "e4.p1"}),
        rel("c4", "coincident", {"e4.p2", "e1.p1"}),
        rel("c5", "horizontal", {"e1"}),
        rel("c6", "horizontal", {"e3"}),
        rel("c7", "vertical", {"e2"}),
        rel("c8", "vertical", {"e4"}),
        rel("c9", "fix", {"e1.p1"}),
        dim("c10", {"e1.p1", "e1.p2"}, W),
        dim("c11", {"e2.p1", "e2.p2"}, H),
    });
    json doc;
    doc["features"] = json::array({
        json{{"id", "s1"}, {"type", "sketch"}, {"plane", "top"},
             {"entities", entities}, {"constraints", constraints}},
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 25.0}},
    });
    return doc;
}

// Call the DLL, parse the JSON result, hand back rc. Frees the DLL string.
int call(const std::string& method, const json& params, json& out, int& rc) {
    char* result = nullptr;
    rc = g_call(method.c_str(), params.dump().c_str(), &result);
    if (!result) return fail("chudware_call returned null out_json");
    std::string s(result);
    g_free(result);
    try {
        out = json::parse(s);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "[probe] result not JSON: %s\n", e.what());
        return 1;
    }
    return 0;
}

}  // namespace

int main() {
#if defined(_WIN32)
    HMODULE dll = LoadLibraryW(L"libchudware.dll");
    if (!dll) {
        std::fprintf(stderr, "[probe] LoadLibrary(libchudware.dll) failed: %lu\n", GetLastError());
        return 2;
    }
    g_call = reinterpret_cast<chudware_call_fn>(GetProcAddress(dll, "chudware_call"));
    g_free = reinterpret_cast<chudware_free_fn>(GetProcAddress(dll, "chudware_free"));
    g_abi = reinterpret_cast<chudware_abi_fn>(GetProcAddress(dll, "chudware_abi_version"));
#else
    // POSIX: dlopen libchudware.so. Prefer CHUDWARE_LIBCHUDWARE, else the copy next to this exe
    // (probe + lib are co-located in bin/), else the loader's default search.
    std::string libpath = "libchudware.so";
    if (const char* env = std::getenv("CHUDWARE_LIBCHUDWARE"); env && env[0]) {
        libpath = env;
    } else {
        char buf[4096];
        ssize_t n = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
        if (n > 0) {
            buf[n] = '\0';
            std::string p(buf);
            size_t slash = p.find_last_of('/');
            if (slash != std::string::npos) libpath = p.substr(0, slash) + "/libchudware.so";
        }
    }
    void* dll = dlopen(libpath.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!dll) {
        std::fprintf(stderr, "[probe] dlopen(%s) failed: %s\n", libpath.c_str(), dlerror());
        return 2;
    }
    g_call = reinterpret_cast<chudware_call_fn>(dlsym(dll, "chudware_call"));
    g_free = reinterpret_cast<chudware_free_fn>(dlsym(dll, "chudware_free"));
    g_abi = reinterpret_cast<chudware_abi_fn>(dlsym(dll, "chudware_abi_version"));
#endif
    if (!g_call || !g_free || !g_abi) return fail("symbol lookup for an export failed");

    std::printf("[probe] libchudware ABI version: %s\n", g_abi());

    // 1. ping
    {
        json out;
        int rc = 0;
        if (call("ping", json::object(), out, rc) != 0) return 1;
        if (rc != 0) return fail("ping returned nonzero");
        if (!out.value("ok", false)) return fail("ping ok != true");
        std::printf("[probe] ping ok\n");
    }

    // 2. doc_compile_cpp on a box -> one solid body, all features ok, AICM bytes.
    {
        json params;
        params["doc"] = box_doc();
        params["env"] = json::object();
        json out;
        int rc = 0;
        if (call("doc_compile_cpp", params, out, rc) != 0) return 1;
        if (rc != 0) {
            std::fprintf(stderr, "[probe] doc_compile_cpp rc=%d out=%s\n", rc, out.dump().c_str());
            return fail("doc_compile_cpp returned nonzero");
        }
        if (!out.value("ok", false)) return fail("compile ok != true");
        if (out.value("body_count", 0) != 1) return fail("expected body_count == 1");
        if (out.value("aicm_bytes", (uint64_t)0) == 0) return fail("expected AICM bytes > 0");
        for (const auto& s : out["statuses"]) {
            if (!s.value("ok", false)) {
                std::fprintf(stderr, "[probe] feature %s error: %s\n",
                             s.value("id", "?").c_str(), s.value("error", "").c_str());
                return fail("a feature did not compile ok");
            }
        }
        std::printf("[probe] doc_compile_cpp ok: body_count=%d aicm_bytes=%llu path=%s\n",
                    out.value("body_count", 0),
                    (unsigned long long)out.value("aicm_bytes", (uint64_t)0),
                    out.value("aicm_path", "").c_str());
    }

    // 3. unknown method -> nonzero (fall-back signal) with an {"error":...} payload.
    {
        json out;
        int rc = 0;
        if (call("no_such_method", json::object(), out, rc) != 0) return 1;
        if (rc == 0) return fail("unknown method should return nonzero");
        if (!out.contains("error")) return fail("unknown method should carry an error payload");
        std::printf("[probe] unknown-method fallback signal ok (rc=%d)\n", rc);
    }

    // 4. ecad_new -> the C++ ECAD core mints a board doc. Validates the ECAD seam that the
    //    Linux host now routes to by default (cppEcadFlag() default-on off-Windows).
    {
        json out;
        int rc = 0;
        if (call("ecad_new", json{{"name", "probe_board"}}, out, rc) != 0) return 1;
        if (rc != 0) {
            std::fprintf(stderr, "[probe] ecad_new rc=%d out=%s\n", rc, out.dump().c_str());
            return fail("ecad_new returned nonzero");
        }
        std::string did = out.value("doc_id", out.value("id", std::string()));
        if (did.empty()) return fail("ecad_new returned no doc_id");
        std::printf("[probe] ecad_new ok: doc_id=%s\n", did.c_str());
    }

    std::printf("[probe] ALL CHECKS PASSED\n");
    return 0;
}
