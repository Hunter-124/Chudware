// libchudware — the C ABI boundary of the pure-C++ core.
//
// This is the SEAM for the strangler migration off the Python engine. The host
// (host/src/worker_client.cpp) today funnels every request through one method —
// WorkerClient::call(method, params, result, err, ...) — to a Python subprocess.
// This header exposes the SAME shape (a JSON-RPC-style string-in / string-out
// dispatch) from an in-process C++ DLL, so the host can route already-ported
// methods here and fall through to Python for the rest. As each feature reaches
// parity it gets cut over and its Python is deleted (the zero-Python cutover).
//
// Why a flat C ABI (not C++ classes across the boundary): a C ABI is stable
// across compilers / CRT versions, so the host (its own MSVC build) and this DLL
// (linked against OCCT) can be built independently and the host can LoadLibrary
// it at runtime. libchudware is a SEPARATE DLL (not static-linked into
// the host exe) precisely so OCCT stays out of the host build and the link is
// fully reversible during migration.
//
// MEMORY OWNERSHIP: every char* returned via an out-param is heap-allocated by
// the DLL and MUST be released with chudware_free(). Never free it with the host's
// own free()/delete — the DLL may use a different allocator (OCCT pulls in
// jemalloc). All strings are null-terminated UTF-8 JSON.
#pragma once

#include <stdint.h>

#if defined(_WIN32)
#  if defined(CHUDWARE_BUILD_DLL)
#    define CHUDWARE_API __declspec(dllexport)
#  else
#    define CHUDWARE_API __declspec(dllimport)
#  endif
#else
#  define CHUDWARE_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

// Dispatch one JSON-RPC-style call into the C++ core.
//
//   method      null-terminated UTF-8 method name (e.g. "doc_compile_cpp").
//   params_json null-terminated UTF-8 JSON object, or NULL/"" for no params.
//   out_json    [out] receives a malloc'd null-terminated UTF-8 JSON string:
//               the RESULT object on success, or {"error":"..."} on failure.
//               The caller owns it and MUST release it with chudware_free().
//
// Returns 0 when the method was recognized and handled (out_json = its result),
// and nonzero when the method is unknown or the call failed (out_json carries an
// {"error":...} payload). The host uses a nonzero return as the signal to fall
// back to the Python worker for that method.
//
// Never throws across the boundary: all C++ exceptions are caught and converted
// to a nonzero return with an {"error":...} payload (mirrors the never-raise
// contract of compile_document / the Python worker).
CHUDWARE_API int chudware_call(const char* method, const char* params_json, char** out_json);

// Release a string previously returned by chudware_call's out_json.
CHUDWARE_API void chudware_free(char* s);

// Live event streaming (transparency). Register a THREAD-LOCAL callback the core invokes with
// each agent-run event's JSON (null-terminated UTF-8) synchronously as it is emitted, so a host
// can surface the run live instead of only the batched result["events"]. The string is owned by
// the core and valid only for the duration of the call — copy it if you keep it. Pass cb=NULL to
// clear. `ud` is passed back verbatim. Set on the SAME thread that will call chudware_call.
CHUDWARE_API void chudware_set_event_sink(void (*cb)(const char* event_json, void* ud), void* ud);

// A monotonically-increasing ABI version string (e.g. "1"). Lets the host detect
// a stale/incompatible libchudware.dll before routing real calls to it.
CHUDWARE_API const char* chudware_abi_version(void);

// The git-describe build id captured at CMake configure time (same value
// chudware-hostd reports as its own build id — both are configured from ONE
// version header, so a hostd/libchudware pair from the same build always match).
// Drives the version drain-replace staleness check (see docs/reference/HOSTD.md).
CHUDWARE_API const char* chudware_build_id(void);


// Live document publication for doc_new/doc_apply_ops calls made synchronously inside an opaque
// agent RPC. Like chudware_set_event_sink, this callback is THREAD-LOCAL and valid only for the
// surrounding chudware_call. JSON strings are borrowed for the callback duration. Pass cb=NULL
// to clear. Optional for hosts: older hosts simply retain end-of-run document synchronization.
CHUDWARE_API void chudware_set_doc_publish_sink(
    void (*cb)(const char* method, const char* params_json, const char* result_json, void* ud),
    void* ud);

// Process-wide asynchronous cache verification publication, outside document locks.
CHUDWARE_API void chudware_set_cache_refresh_sink(
    void (*cb)(const char* doc_id, const char* response_json, void* ud), void* ud);

// Private lifecycle census, not a chudware_call method. No allocations or document
// copies. Counts are lower bounds; nonzero return means incomplete (never idle).
// counts[0..2] = cache verification, profiler, dirty documents.
CHUDWARE_API int chudware_activity(uint64_t counts[3]);

#ifdef __cplusplus
}  // extern "C"
#endif

// Engine-only linear buckling uses the same public C entry as modal:
// chudware_call("sim_run_buckling", "{\"study\":{...},\"n_modes\":4}", &out).
// The study is the static mesh/material/fixture/load shape; results and in-band
// refusals are JSON at rc 0, and out is released with chudware_free().
// See contracts/worker_rpc.md section 27 for mode/participation/evidence fields.
// Optional top-level max_load_factor (default 10000) bounds the buckling search;
// no mode below that ceiling is an in-band refusal, not infinite strength.
