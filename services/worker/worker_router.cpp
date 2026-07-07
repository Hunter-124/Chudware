#include "worker_router.h"
#include <cstdlib>
#include <cstring>

namespace aicad::services {

bool cppDocsEnabled() {
    // Default ON, matching worker_client.cpp cppDocsFlag() (C++ doc engine is
    // parity-complete since 2026-06-19). AI_CAD_CPP_DOCS=0 forces the Python oracle.
    const char* v = std::getenv("AI_CAD_CPP_DOCS");
    if (v && *v) return std::strcmp(v, "0") != 0;
    return true;
}

bool isCppDocId(const std::string& docId) {
    return docId.rfind("doc-cpp", 0) == 0;
}

EngineBackend routeMethod(const std::string& method, const std::string& docId) {
    if (!cppDocsEnabled())
        return EngineBackend::PythonOracle;
    // Minting methods create the doc session in the core (no docId exists yet).
    // doc_open was missing here, so opening any file from the GUI fell through to
    // the Python oracle -> "unknown method: doc_open" toast spam at startup.
    if (method == "doc_new" || method == "doc_open")
        return EngineBackend::CppCore;
    if (!docId.empty() && isCppDocId(docId))
        return EngineBackend::CppCore;
    if (method.rfind("doc_", 0) == 0 && !docId.empty() && isCppDocId(docId))
        return EngineBackend::CppCore;
    return EngineBackend::PythonOracle;
}

} // namespace aicad::services
