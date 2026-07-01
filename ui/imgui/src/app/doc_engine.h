// WorkerClient-backed engine facade — same eng::Engine surface the panels already use.
// Replaces the direct libaicad cpp_core shortcut in engine/engine.cpp.
#pragma once
#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <nlohmann/json.hpp>

class WorkerClient;
namespace core { class DocMirror; struct OpsResponse; }

namespace eng {

inline nlohmann::json dim(double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; }

struct OpsResult {
    bool ok = false;
    int  revision = -1;
    nlohmann::json tree;
    nlohmann::json featureStatus;
    std::string aicmPath;
    std::string errCode, errMsg;
};

class Engine {
public:
    Engine(WorkerClient& worker, core::DocMirror& mirror);

    bool available();
    bool bridgeCompiled() const { return true; }
    const std::string& lastError() const { return lastErr_; }

    bool hasDoc() const;
    const std::string& docId() const;
    int revision() const;
    const nlohmann::json& tree() const;
    const nlohmann::json& featureStatus() const;
    const std::string& aicmPath() const;

    bool newPart(const std::string& name = "Part");
    bool save(const std::string& path);
    bool open(const std::string& path);
    OpsResult apply(const nlohmann::json& ops, bool coalesce = false);
    OpsResult addFeature(const nlohmann::json& feature);
    OpsResult refreshTree();
    OpsResult rebuild();
    OpsResult undo();
    OpsResult redo();

    OpsResult addBox(double l, double w, double h, const std::string& name = "Box");
    OpsResult addCylinder(double r, double h, const std::string& name = "Cylinder");
    OpsResult addSphere(double r, const std::string& name = "Sphere");
    OpsResult addCone(double r1, double r2, double h, const std::string& name = "Cone");
    OpsResult addFilletAll(double radius, const std::string& name = "Fillet");
    OpsResult addChamferAll(double length, const std::string& name = "Chamfer");
    OpsResult addShell(double thickness, const std::string& name = "Shell");
    OpsResult addDraftSides(double angleDeg, const std::string& name = "Draft");
    OpsResult addHoleCenter(double diameter, const std::string& name = "Hole");

    nlohmann::json resolvePick(uint32_t body_id, const std::string& kind, uint32_t topo_id);
    OpsResult addShellFaces(double thickness, const nlohmann::json& removeRefs);
    OpsResult addDraftFaces(double angleDeg, const nlohmann::json& faceRefs);
    OpsResult addFilletEdges(double radius, const nlohmann::json& edgeRefs);
    OpsResult addChamferEdges(double length, const nlohmann::json& edgeRefs);

    OpsResult sketchRectExtrude(const std::string& plane, double w, double h, double depth,
                                const std::string& mode = "boss");
    OpsResult sketchCircleExtrude(const std::string& plane, double r, double depth,
                                  const std::string& mode = "boss");

    void startAgent(const std::string& request);
    bool agentRunning() const { return agentRunning_.load(); }
    bool agentDone() const { return agentDone_.load(); }
    nlohmann::json takeAgentResult();

private:
    OpsResult fromMirror(const struct core::OpsResponse& o);
    OpsResult call(const std::string& method, const nlohmann::json& params);
    OpsResult parse(const nlohmann::json& res);
    void ingest(const OpsResult& r);

    WorkerClient& worker_;
    core::DocMirror& mirror_;
    std::string lastErr_;
    std::string aicmPath_;

    std::atomic<bool> agentRunning_{false};
    std::atomic<bool> agentDone_{false};
    nlohmann::json agentResult_;
    mutable std::mutex agentMtx_;
    std::thread agentThread_;
};

}  // namespace eng
