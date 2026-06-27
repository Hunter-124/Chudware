// Engine — the new GUI's bridge to the AI-CAD C++ kernel (libaicad.dll), via the reused
// flat C ABI (core::cpp_core / aicad_call). Doc operations route to the C++ core by default
// (WorkerClient's cppDocsFlag), so we drive a real parametric doc engine WITHOUT spawning the
// Python worker — just load the DLL. Header stays light (JSON only) so UI TUs don't pull windows.h.
#pragma once
#include <string>
#include <cstdint>
#include <nlohmann/json.hpp>

namespace eng {

// Dimension param objects on the wire are {"value":<num>,"expr":null}.
inline nlohmann::json dim(double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; }

struct OpsResult {
    bool ok = false;
    int  revision = -1;
    nlohmann::json tree;            // aicad.part/1 doc JSON (has "features":[...])
    nlohmann::json featureStatus;   // [{id,ok,ms,error,dangling_refs}]
    std::string aicmPath;           // tess.aicm_path (mesh file) when present
    std::string errCode, errMsg;
};

class Engine {
public:
    bool available();                          // lazily load libaicad.dll (false if unavailable)
    bool bridgeCompiled() const;               // built with the C++ bridge at all
    const std::string& lastError() const { return lastErr_; }

    bool hasDoc() const { return !docId_.empty(); }
    const std::string& docId() const { return docId_; }
    int revision() const { return revision_; }
    const nlohmann::json& tree() const { return tree_; }
    const nlohmann::json& featureStatus() const { return featureStatus_; }
    const std::string& aicmPath() const { return aicmPath_; }

    bool newPart(const std::string& name = "Part");
    bool save(const std::string& path);        // doc_save -> .step+ (native editable format)
    bool open(const std::string& path);        // doc_open -> adopt the opened doc (tree+mesh)
    OpsResult apply(const nlohmann::json& ops, bool coalesce = false);
    OpsResult addFeature(const nlohmann::json& feature);
    OpsResult refreshTree();
    OpsResult rebuild();                       // doc_rebuild — forces a fresh tess.aicm_path
    OpsResult undo();
    OpsResult redo();

    // convenience feature builders (return the apply result)
    OpsResult addBox(double l, double w, double h, const std::string& name = "Box");
    OpsResult addCylinder(double r, double h, const std::string& name = "Cylinder");
    OpsResult addSphere(double r, const std::string& name = "Sphere");
    OpsResult addCone(double r1, double r2, double h, const std::string& name = "Cone");
    OpsResult addFilletAll(double radius, const std::string& name = "Fillet");
    OpsResult addChamferAll(double length, const std::string& name = "Chamfer");
    OpsResult addShell(double thickness, const std::string& name = "Shell");
    OpsResult addDraftSides(double angleDeg, const std::string& name = "Draft");
    OpsResult addHoleCenter(double diameter, const std::string& name = "Hole");

    // ---- viewport picking <-> kernel selectors (engine_pick.cpp) ----
    nlohmann::json resolvePick(uint32_t body_id, const std::string& kind, uint32_t topo_id);
    OpsResult addShellFaces(double thickness, const nlohmann::json& removeRefs);  // shell, removing picked faces
    OpsResult addDraftFaces(double angleDeg, const nlohmann::json& faceRefs);

private:
    OpsResult call(const std::string& method, const nlohmann::json& params);
    OpsResult parse(const nlohmann::json& res);
    void ingest(const OpsResult& r);           // adopt revision/tree/status/aicm from a good result

    std::string docId_, aicmPath_, lastErr_;
    int revision_ = -1;
    nlohmann::json tree_, featureStatus_;
};

} // namespace eng
