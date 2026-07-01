#pragma once
// Shared worker/job types (extracted from host core/app_state.h — no ImGui / UI deps).

#include <atomic>
#include <mutex>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

enum JobKind { JOB_BUILD, JOB_DESIGN, JOB_CHAT };

struct PartInfo {
    std::string name, stlPath, dims;
    double volume = 0;
};

struct FitPair {
    std::string a, b;
    double value = 0;
};

struct FitReport {
    bool present = false;
    bool ok = true;
    std::string summary;
    std::vector<FitPair> interferences, clearances;
};

struct PlanPart {
    std::string name, purpose, dims;
    std::vector<std::string> features;
};

struct PlanInfo {
    bool present = false;
    std::string name, summary, fit;
    std::vector<PlanPart> parts;
};

struct AgentEvent {
    std::string kind;
    int index = 0;
    bool ok = false, done = false;
    std::string phase, text, notes, thinking, code, diff, dims, error, contactSheet, stdoutText;
    std::string stlPath;
    bool isAssembly = false;
    std::vector<PartInfo> parts;
    FitReport fit;
    PlanInfo plan;
};

struct JobState {
    std::atomic<bool> running{false};
    std::atomic<bool> ready{false};
    std::mutex mtx;
    int kind = JOB_BUILD;
    std::string stl, info, error;
    bool errorIsCad = false;
    bool isAssembly = false;
    std::vector<PartInfo> parts;
    FitReport fit;
    bool converged = true;
    std::string stopReason;
    std::string createdDocPath;
    std::string drawingDocId;
    std::string drawingName;
    std::string drawingSourceId;
    std::string camDocId;
    bool isMission = false;
    std::string missionVerdict;
    std::mutex evMtx;
    std::vector<AgentEvent> events;
};

inline std::string jstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
inline bool jbool(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return it != o.end() && it->is_boolean() && it->get<bool>();
}
inline int jint(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number_integer()) ? it->get<int>() : 0;
}
inline double jnum(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<double>() : 0.0;
}

std::vector<PartInfo> parseParts(const nlohmann::json& arr);
FitReport parseFit(const nlohmann::json& f);
PlanInfo parsePlan(const nlohmann::json& p);
