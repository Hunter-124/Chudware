// libaicad — the C ABI boundary of the pure-C++ core.
//
// This is the SEAM for the strangler migration off the Python engine. The host
// (host/src/worker_client.cpp) today funnels every request through one method —
// WorkerClient::call(method, params, result, err, ...) — to a Python subprocess.
// This header exposes the SAME shape (a JSON-RPC-style string-in / string-out
// dispatch) from an in-process C++ DLL, so the host can route already-ported
// methods here and fall through to Python for the rest. As each feature reaches
// parity it gets cut over and its Python is deleted (see docs/CPP_MIGRATION.md).
//
// Why a flat C ABI (not C++ classes across the boundary): a C ABI is stable
// across compilers / CRT versions, so the host (its own MSVC build) and this DLL
// (linked against OCCT) can be built independently and the host can LoadLibrary
// it at runtime. The user chose a SEPARATE libaicad.dll (not static-linked into
// the host exe) precisely so OCCT stays out of the host build and the link is
// fully reversible during migration.
//
// MEMORY OWNERSHIP: every char* returned via an out-param is heap-allocated by
// the DLL and MUST be released with aicad_free(). Never free it with the host's
// own free()/delete — the DLL may use a different allocator (OCCT pulls in
// jemalloc). All strings are null-terminated UTF-8 JSON.
#pragma once

#if defined(_WIN32)
#  if defined(AICAD_BUILD_DLL)
#    define AICAD_API __declspec(dllexport)
#  else
#    define AICAD_API __declspec(dllimport)
#  endif
#else
#  define AICAD_API
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
//               The caller owns it and MUST release it with aicad_free().
//
// Returns 0 when the method was recognized and handled (out_json = its result),
// and nonzero when the method is unknown or the call failed (out_json carries an
// {"error":...} payload). The host uses a nonzero return as the signal to fall
// back to the Python worker for that method.
//
// Never throws across the boundary: all C++ exceptions are caught and converted
// to a nonzero return with an {"error":...} payload (mirrors the never-raise
// contract of compile_document / the Python worker).
AICAD_API int aicad_call(const char* method, const char* params_json, char** out_json);

// Release a string previously returned by aicad_call's out_json.
AICAD_API void aicad_free(char* s);

// Live event streaming (transparency). Register a THREAD-LOCAL callback the core invokes with
// each agent-run event's JSON (null-terminated UTF-8) synchronously as it is emitted, so a host
// can surface the run live instead of only the batched result["events"]. The string is owned by
// the core and valid only for the duration of the call — copy it if you keep it. Pass cb=NULL to
// clear. `ud` is passed back verbatim. Set on the SAME thread that will call aicad_call.
AICAD_API void aicad_set_event_sink(void (*cb)(const char* event_json, void* ud), void* ud);

// A monotonically-increasing ABI version string (e.g. "1"). Lets the host detect
// a stale/incompatible libaicad.dll before routing real calls to it.
AICAD_API const char* aicad_abi_version(void);

// The git-describe build id captured at CMake configure time (same value
// aicad-hostd reports as its own build id — both are configured from ONE
// version header, so a hostd/libaicad pair from the same build always match).
// Drives the version drain-replace staleness check (see docs/HOSTD.md).
AICAD_API const char* aicad_build_id(void);

#ifdef __cplusplus
}  // extern "C"
#endif
