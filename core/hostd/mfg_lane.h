#pragma once

#include <atomic>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

#include "device/emulator_transport.h"
#include "device/grbl_adapter.h"
#include "manufacturing/arbiter.h"
#include "manufacturing/invalidation.h"
#include "manufacturing/lease.h"
#include "manufacturing/result_registry.h"

namespace chudware::mfg {

struct LaneRpcResult {
    nlohmann::json body;
    bool is_error = false;
};

class ManufacturingLane {
public:
    using EventSink = std::function<void(const std::string&)>;

    explicit ManufacturingLane(std::filesystem::path state_directory,
                               bool emulator_endpoint_is_physical = false);
    ~ManufacturingLane();

    ManufacturingLane(const ManufacturingLane&) = delete;
    ManufacturingLane& operator=(const ManufacturingLane&) = delete;

    std::optional<LaneRpcResult> handle(std::string_view method,
                                        const nlohmann::json& params);
    void setEventSink(EventSink sink);

private:
    class SteadyClock final : public Clock {
    public:
        uint64_t mono_ns() const override;
        void advance_ns(uint64_t amount) { offset_ns_.fetch_add(amount); }

    private:
        std::atomic<uint64_t> offset_ns_{0};
    };

    struct ArtifactRecord {
        std::string artifact_id;
        std::string device_id;
        tier0::ArtifactKind kind = tier0::ArtifactKind::Nc;
        std::string hash_sha256;
        uint64_t bytes = 0;
        std::vector<std::string> program_lines;
        std::string post_profile_id;
        std::string post_profile_hash;
        std::string slicer_profile_hash;
        device::Seq seq = 0;
    };

    struct RunRecord {
        std::string run_id;
        std::string device_id;
        std::string artifact_id;
        device::RunState state = device::RunState::Draft;
        ApprovalJob job;
        ApprovalSnapshot snapshot;
        std::string gate_report_ref;
        std::string approval_id;
        std::optional<InitiatorProvenance> initiator;
        device::Seq created_at_seq = 0;
    };

    struct ApprovalProposal {
        std::string proposal_id;
        std::string run_id;
        ExercisableBy exercisable_by = ExercisableBy::HumanOnly;
        uint64_t expiry_ttl_ms = 300000;
        ApprovalAttendance attendance;
        SpindleAuthority spindle_authority = SpindleAuthority::NotApplicable;
        device::Seq seq = 0;
    };

    LaneRpcResult route(std::string_view method, const nlohmann::json& params);
    LaneRpcResult ok(nlohmann::json fields = nlohmann::json::object()) const;
    LaneRpcResult refuse(std::string code, std::string message,
                         nlohmann::json detail = nlohmann::json::object()) const;

    bool validDevice(const nlohmann::json& params, LaneRpcResult& refusal) const;
    bool requireLease(const nlohmann::json& params, LaneRpcResult& refusal);
    device::Seq appendEvent(std::string kind, std::string subject,
                            nlohmann::json body, nlohmann::json raw = nullptr);
    void publishEvent(std::string_view kind, std::string_view subject,
                      device::Seq seq, const nlohmann::json& body);
    void serviceDevice();
    device::CapabilityReport capabilityReport() const;
    FreshValue<device::DeviceState> deviceState() const;
    nlohmann::json artifactJson(const ArtifactRecord& artifact) const;
    nlohmann::json runJson(const RunRecord& run) const;
    nlohmann::json proposalJson(const ApprovalProposal& proposal) const;
    nlohmann::json approvalJson(const ApprovalBinding& approval) const;
    nlohmann::json gateReportJson(const gates::GateReport& report) const;
    gates::GateReport computePreflight(RunRecord& run, const ArtifactRecord& artifact);
    RunAuthorization authorizationFor(const RunRecord& run,
                                      const ArtifactRecord& artifact,
                                      const ApprovalBinding& approval) const;

    static tier0::ArtifactKind parseArtifactKind(std::string_view value);
    static ExercisableBy parseExercisableBy(std::string_view value);
    static AttendanceMode parseAttendanceMode(std::string_view value);
    static SpindleAuthority parseSpindleAuthority(std::string_view value);
    static Initiator parseInitiator(const nlohmann::json& value);

    mutable std::mutex mutex_;
    EventSink event_sink_;
    device::LaneEpoch epoch_;
    Journal journal_;
    SteadyClock clock_;
    NonceLedger nonces_;
    Arbiter arbiter_;
    LeaseRegistry leases_;
    ResultRegistry result_registry_;
    device::ControllerIdentitySnapshot advertised_identity_;
    device::MachineFingerprint advertised_fingerprint_;
    device::DeviceId device_id_;
    device::Endpoint endpoint_;
    device::EmulatorTransport emulator_;
    device::GrblAdapter adapter_;
    bool connected_ = false;
    std::map<std::string, ArtifactRecord, std::less<>> artifacts_;
    std::map<std::string, RunRecord, std::less<>> runs_;
    std::map<std::string, ApprovalProposal, std::less<>> proposals_;
    std::map<std::string, ApprovalBinding, std::less<>> approvals_;
    std::map<std::string, gates::GateReport, std::less<>> gate_reports_;
    std::vector<InvalidationNotice> invalidations_;
};

ManufacturingLane& lane();

}  // namespace chudware::mfg
