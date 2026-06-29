#pragma once
// Routing policy for the worker seam: libaicad (C++) vs engine_oracle (Python).
// See contracts/worker_rpc.md and docs/CPP_MIGRATION.md.
#include <string>

namespace aicad::services {

enum class EngineBackend { CppCore, PythonOracle };

// Returns which backend should handle a JSON-RPC method + optional doc id.
EngineBackend routeMethod(const std::string& method, const std::string& docId = {});

// True when AI_CAD_CPP_DOCS is enabled and doc id carries the doc-cpp prefix.
bool cppDocsEnabled();
bool isCppDocId(const std::string& docId);

} // namespace aicad::services
