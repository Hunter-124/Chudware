#include "worker_router.h"
#include <cstdlib>
#include <cstring>

namespace chudware::services {

bool cppDocsEnabled() {
    // R5a (T-RF21): unconditional — the C++ doc engine is the only backend.
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

} // namespace chudware::services
