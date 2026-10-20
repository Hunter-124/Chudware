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

#include "device/grbl_adapter.h"
#include "manufacturing/arbiter.h"
#include "manufacturing/gateway/device_allowlist.h"
#include "manufacturing/invalidation.h"
#include "manufacturing/lease.h"
#include "manufacturing/result_registry.h"

namespace chudware::mfg {

struct LaneRpcResult {
    nlohmann::json body;
    bool is_error = false;
};

// Server-owned admission limits; constructor injection allows small boundary fixtures.
struct ArtifactLimits {
    size_t max_objects = 256;
    uint64_t max_retained_bytes = 64ULL * 1024 * 1024;
    uint64_t max_artifact_bytes = 8ULL * 1024 * 1024;
    size_t max_artifact_lines = 100000;
};

class ManufacturingLane {
public:
    using EventSink = std::function<void(const std::string&)>;
    using WorkerCall =
        std::function<nlohmann::json(const std::string&, const nlohmann::json&)>;

    explicit ManufacturingLane(std::filesystem::path state_directory,
                               bool emulator_endpoint_is_physical = false,
                               std::function<uint64_t()> monotonic_clock = {},
                               ArtifactLimits artifact_limits = {});
    ~ManufacturingLane();

    ManufacturingLane(const ManufacturingLane&) = delete;
    ManufacturingLane& operator=(const ManufacturingLane&) = delete;

    std::optional<LaneRpcResult> handle(std::string_view method,
                                        const nlohmann::json& params,
                                        const LeaseOwner& caller);
    void setEventSink(EventSink sink);
    void setWorkerCall(WorkerCall call);

private:
    class SteadyClock final : public Clock {
    public:
        explicit SteadyClock(std::function<uint64_t()> source = {});
        uint64_t mono_ns() const override;
        void advance_ns(uint64_t amount) { offset_ns_.fetch_add(amount); }
    private:
        std::function<uint64_t()> source_;
        std::atomic<uint64_t> offset_ns_{0};
    };

    struct ArtifactRecord {
        std::string artifact_id;
        std::string device_id;
        tier0::ArtifactKind kind = tier0::ArtifactKind::Nc;
        std::string hash_sha256;
        uint64_t bytes = 0;
        uint64_t retained_bytes = 0;
        size_t run_references = 0;
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
        device::RunDiscontinuity discontinuity = device::RunDiscontinuity::None;
        bool completion_observed = false;
        uint64_t hold_requested_at_ns = 0;
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

    struct DeviceCandidate {
        std::string device_id;
        device::MachineClass machine_class = device::MachineClass::Emulated;
        device::TransportKind transport_kind = device::TransportKind::Emulator;
        device::Endpoint endpoint;
        bool allowlisted = false;
        std::optional<gateway::AllowlistEntry> allowlist_entry;
    };

    LaneRpcResult route(std::string_view method, const nlohmann::json& params,
                        const LeaseOwner& caller);
    LaneRpcResult ok(nlohmann::json fields = nlohmann::json::object()) const;
    LaneRpcResult refuse(std::string code, std::string message,
                         nlohmann::json detail = nlohmann::json::object()) const;

    std::vector<DeviceCandidate> deviceCandidates() const;
    std::optional<DeviceCandidate> findCandidate(std::string_view device_id) const;
    void selectTransport(const DeviceCandidate& candidate);
    void invalidateSelectedDeviceRecords(std::string_view previous_device_id);
    void reclaimUnreferencedArtifacts(std::string_view device_id);
    void observeMachineIdentity(device::ControllerIdentitySnapshot identity,
                                device::MachineFingerprint fingerprint);
    void failExpiredClaimSafely();
    MachinePolicy lanePolicy() const;
    Initiator authenticatedInitiator(const LeaseOwner& caller) const;
    void reconcileAllowlistEpoch();
    bool isPrinter() const;
    nlohmann::json workerParams() const;
    std::optional<nlohmann::json> callWorker(const std::string& method,
                                             const nlohmann::json& params,
                                             LaneRpcResult& refusal);
    nlohmann::json capabilityJson() const;
    void closeActiveDevice(std::string_view why);
    void restoreAttention();
    void applyAttention(const JournalRecord& record);
    bool validDevice(const nlohmann::json& params, LaneRpcResult& refusal) const;
    bool requireLease(const nlohmann::json& params, const LeaseOwner& caller,
                      LaneRpcResult& refusal);
    bool requireCommander(const LeaseOwner& caller, LaneRpcResult& refusal);
    device::Seq appendEvent(std::string kind, std::string subject,
                            nlohmann::json body, nlohmann::json raw = nullptr);
    void publishEvent(std::string_view kind, std::string_view subject,
                      device::Seq seq, const nlohmann::json& body);
    void serviceDevice();
    device::CapabilityReport capabilityReport() const;
    void transitionRun(RunRecord& run, device::RunState state, std::string_view cause);
    void raiseRunAttention(std::string_view run_id, std::string_view reason,
                           std::string_view why);
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

    mutable std::mutex mutex_;
    EventSink event_sink_;
    WorkerCall worker_call_;
    device::LaneEpoch epoch_;
    Journal journal_;
    gateway::DeviceAllowlist allowlist_;
    std::uint64_t observed_allowlist_epoch_ = 0;
    SteadyClock clock_;
    NonceLedger nonces_;
    Arbiter arbiter_;
    LeaseRegistry leases_;
    ResultRegistry result_registry_;
    device::ControllerIdentitySnapshot emulator_identity_;
    device::MachineFingerprint emulator_fingerprint_;
    device::ControllerIdentitySnapshot current_identity_;
    std::optional<device::MachineFingerprint> current_machine_fingerprint_;
    device::DeviceId device_id_;
    device::Endpoint endpoint_;
    device::MachineClass machine_class_ = device::MachineClass::Emulated;
    device::TransportKind transport_kind_ = device::TransportKind::Emulator;
    std::optional<gateway::AllowlistEntry> selected_allowlist_entry_;
    std::unique_ptr<device::ITransport> transport_;
    std::unique_ptr<device::GrblAdapter> adapter_;
    nlohmann::json printer_capability_;
    bool connected_ = false;
    bool terminal_claim_ = false;
    std::optional<CommandLease> active_claim_;
    std::map<std::string, ArtifactRecord, std::less<>> artifacts_;
    const ArtifactLimits artifact_limits_;
    uint64_t retained_artifact_bytes_ = 0;
    std::map<std::string, RunRecord, std::less<>> runs_;
    std::map<std::string, ApprovalProposal, std::less<>> proposals_;
    std::map<std::string, ApprovalBinding, std::less<>> approvals_;
    std::map<std::string, gates::GateReport, std::less<>> gate_reports_;
    std::vector<InvalidationNotice> invalidations_;
    std::map<std::string, nlohmann::json, std::less<>> attention_;
};

// Lazily constructs the process-wide lane. Returns nullptr if construction
// failed (e.g. the manufacturing journal already has a writer); the failure
// reason is copied into *init_error when provided. Never throws.
ManufacturingLane* tryLane(std::string* init_error = nullptr);

}  // namespace chudware::mfg
