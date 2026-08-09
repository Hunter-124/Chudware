#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>
#include <utility>

#include <nlohmann/json.hpp>
#include "attention_panel.h"

namespace ui {

// Read-only projection of the gateway's manufacturing lane. This type never
// computes a controller/run transition, gate verdict, or approval binding.
enum class MfgFreshState { Ok, Unknown, Stale, Refused, Unsupported };

struct MfgLastGood {
    nlohmann::json value;
    std::uint64_t tMonoNs = 0;
    std::uint64_t seq = 0;
};

struct MfgFreshValue {
    nlohmann::json value;
    bool hasValue = false;
    MfgFreshState state = MfgFreshState::Unknown;
    std::uint64_t tMonoNs = 0;
    std::uint64_t seq = 0;
    std::uint64_t ageMs = 0;
    std::optional<std::uint64_t> ttlMs;
    std::string source;
    std::string why = "not reported";
    std::optional<MfgLastGood> lastGood;

    bool current() const { return state == MfgFreshState::Ok && hasValue; }
};

// telemetry=true applies the external telemetry rule that ttl_ms:null cannot
// be painted as current. Durable authored rows pass telemetry=false.
MfgFreshValue parseMfgFresh(const nlohmann::json& wire, bool telemetry);

struct MfgFreshPresentation {
    std::string current;
    std::string detail;
    bool ok = false;
    bool stale = false;
};

// Sole formatting rule used by the device/run panels. Non-ok values always
// render an em dash plus why; last-good is visibly demoted and never current.
MfgFreshPresentation presentMfgFresh(const MfgFreshValue& fresh,
                                     const std::string& unit = {});

struct MfgCapabilityLevel {
    std::string level;
    std::string truth;
    std::string missingLevel;
    std::string why;
    bool reported = false;
};

struct MfgDevice {
    std::string deviceId;
    std::string machineClass;
    std::string controller;
    std::string fingerprint;
    std::string commandOwner;
    std::string leaseId;
    std::string maxExercisableBy = "human_only";
    MfgFreshValue state;
    MfgFreshValue snapshot;
    std::vector<MfgCapabilityLevel> capabilities;
    nlohmann::json policy;
    nlohmann::json transport;
};

struct MfgWaiver {
    std::string by;
    std::string reason;
    std::uint64_t atSeq = 0;
};

struct MfgGateGuard {
    std::string name;
    std::string verdict;
    std::string why;
    std::optional<MfgWaiver> waiver;
};

struct MfgGateReport {
    std::string reportRef;
    std::string stage1;
    std::string stage1Why;
    std::uint64_t seq = 0;
    std::vector<MfgGateGuard> guards;
};

struct MfgCommandAffordance {
    bool enabled = false;
    std::string reason = "Gateway has not supplied an enablement decision.";
};

struct MfgRun {
    std::string runId;
    std::string deviceId;
    std::string approvalId;
    std::string proposalId;
    std::string artifactHash;
    std::string initiatedBy;
    std::string stateLabel;
    MfgFreshValue state;
    MfgFreshValue progress;
    MfgFreshValue snapshot;
    MfgGateReport gates;
    MfgCommandAffordance start;
    MfgCommandAffordance hold;
};

struct MfgApproval {
    std::string approvalId;
    std::string proposalId;
    std::string status = "gathering";
    std::string blockedReason = "The gateway has not produced a binding.";
    nlohmann::json binding;
};

class MfgMirror {
public:
    void ingestRpc(const std::string& method, const nlohmann::json& result);
    void feedEvent(const nlohmann::json& event);
    void markGatewayError(std::string why);
    void seedManufactureShot();

    bool gatewayReachable() const { return gatewayReachable_; }
    const std::string& gatewayWhy() const { return gatewayWhy_; }
    std::uint64_t epoch() const { return epoch_; }
    std::uint64_t latestSeq() const { return latestSeq_; }

    const std::vector<MfgDevice>& devices() const { return devices_; }
    const std::vector<MfgRun>& runs() const { return runs_; }
    const MfgApproval& approval() const { return approval_; }
    const std::vector<MfgAttentionRow>& attention() const { return attention_; }

    MfgDevice* device(const std::string& id);
    const MfgDevice* device(const std::string& id) const;
    MfgRun* run(const std::string& id);
    const MfgRun* run(const std::string& id) const;

private:
    MfgDevice& upsertDevice(const std::string& id);
    MfgRun& upsertRun(const std::string& id);
    void ingestDevice(const nlohmann::json& row);
    void ingestCapability(MfgDevice& device, const nlohmann::json& report);
    void ingestRun(const nlohmann::json& row);
    void ingestGate(MfgGateReport& gate, const nlohmann::json& report);
    void ingestApproval(const nlohmann::json& row);
    void ingestAttentionList(const nlohmann::json& result);
    void ingestAttentionEvent(const nlohmann::json& event, std::uint64_t seq);
    void rebuildAttention();
    void invalidateTelemetry(const std::string& why);

    bool gatewayReachable_ = false;
    std::string gatewayWhy_ = "Manufacturing gateway has not answered yet.";
    std::uint64_t epoch_ = 0;
    std::uint64_t latestSeq_ = 0;
    std::vector<MfgDevice> devices_;
    std::vector<MfgRun> runs_;
    MfgApproval approval_;
    std::uint64_t attentionBaselineSeq_ = 0;
    std::vector<MfgAttentionRow> attentionBaseline_;
    std::vector<std::pair<std::uint64_t, nlohmann::json>> attentionEvents_;
    std::vector<MfgAttentionRow> attention_;
};

}  // namespace ui
