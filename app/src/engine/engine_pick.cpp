// Engine methods that bridge viewport picking <-> kernel selectors. In a SEPARATE TU from
// engine.cpp (member functions may be split across translation units) so it composes cleanly.
#include "engine/engine.h"

#if defined(AICAD_HAVE_BRIDGE)
#  include "src/core/cpp_core.h"
#endif

using nlohmann::json;

namespace eng {

// Resolve a viewport pick (body_id + topo_id) to a stable engine selector object.
// Returns a null json on failure (lastError set).
json Engine::resolvePick(uint32_t body_id, const std::string& kind, uint32_t topo_id) {
#if defined(AICAD_HAVE_BRIDGE)
    if (docId_.empty()) { lastErr_ = "no document"; return json(); }
    json result; std::string err;
    json params = {{"doc_id", docId_}, {"revision", revision_},
                   {"body_id", body_id}, {"kind", kind}, {"topo_id", (int)topo_id}};
    bool ok = core::cpp_core().call("doc_resolve_pick", params, result, err);
    if (!ok && !(result.is_object() && result.contains("code"))) { lastErr_ = err; return json(); }
    if (result.is_object() && result.contains("code")) { lastErr_ = result.value("message", std::string("resolve failed")); return json(); }
    if (result.contains("selector")) return result["selector"];
    return json();
#else
    (void)body_id; (void)kind; (void)topo_id; lastErr_ = "no bridge"; return json();
#endif
}

OpsResult Engine::addShellFaces(double thickness, const json& removeRefs) {
    return addFeature(json{{"type","shell"},{"name","Shell"},
        {"params",{{"thickness",dim(thickness)}}},
        {"refs",{{"remove",removeRefs}}}});
}

OpsResult Engine::addDraftFaces(double angleDeg, const json& faceRefs) {
    return addFeature(json{{"type","draft"},{"name","Draft"},
        {"params",{{"angle",dim(angleDeg)}}},
        {"refs",{{"neutral",{{"of","face"},{"side","bottom"}}},{"faces",faceRefs}}}});
}

} // namespace eng
