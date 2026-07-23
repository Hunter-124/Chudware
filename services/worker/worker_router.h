#pragma once
// Routing policy for the worker seam: libchudware (C++) vs engine_oracle (Python).
// See contracts/worker_rpc.md and docs/CPP_MIGRATION.md.
#include <string>

namespace chudware::services {

enum class EngineBackend { CppCore, PythonOracle };

// Returns which backend should handle a JSON-RPC method + optional doc id.
EngineBackend routeMethod(const std::string& method, const std::string& docId = {});

// True for the (now unconditional) C++ doc engine when the doc id carries the doc-cpp prefix.
bool cppDocsEnabled();
bool isCppDocId(const std::string& docId);

} // namespace chudware::services
