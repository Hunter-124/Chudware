#include "worker_router.h"
#include <cstdlib>
#include <cstring>

namespace aicad::services {

bool cppDocsEnabled() {
    const char* v = std::getenv("AI_CAD_CPP_DOCS");
    return v && *v && std::strcmp(v, "0") != 0;
}

bool isCppDocId(const std::string& docId) {
    return docId.rfind("doc-cpp", 0) == 0;
}

EngineBackend routeMethod(const std::string& method, const std::string& docId) {
    if (!cppDocsEnabled())
        return EngineBackend::PythonOracle;
    if (method == "doc_new")
        return EngineBackend::CppCore;
    if (!docId.empty() && isCppDocId(docId))
        return EngineBackend::CppCore;
    if (method.rfind("doc_", 0) == 0 && !docId.empty() && isCppDocId(docId))
        return EngineBackend::CppCore;
    return EngineBackend::PythonOracle;
}

} // namespace aicad::services
