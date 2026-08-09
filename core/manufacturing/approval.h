#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include "device/device_id.h"
#include "manufacturing/gates.h"
#include "manufacturing/policy.h"

namespace chudware::mfg {

enum class NonceState { Unburned, Burned, Void };

struct ApprovalMachine {
    std::string device_id;
    std::string fingerprint_sha256;
};

struct ApprovalArtifact {
    std::string hash_sha256;
    uint64_t bytes = 0;
    tier0::ArtifactKind kind = tier0::ArtifactKind::Nc;
    std::string post_profile_id;
    std::string post_profile_hash;
    std::string slicer_profile_hash;
};

struct ApprovalJob {
    std::string job_id;
    uint64_t setup_revision = 0;
    device::Seq setup_revision_seq = 0;
    uint32_t quantity = 1;
};

struct ApprovalWcs {
    std::string system;
    std::vector<double> offset;
    std::string established_by;
};

struct ApprovalSnapshot {
    std::string tool_table_hash;
    std::string fixture_hash;
    ApprovalWcs wcs;
};

struct ApprovalPreflight {
    std::vector<std::string> evidence;
    std::string gate_report;
    device::Seq established_at_seq = 0;
};

struct Approver {
    std::string identity;
    std::string auth;
    uint64_t at_mono_ns = 0;
};

struct ApprovalExpiry {
    uint64_t t_mono_ns = 0;
    uint64_t ttl_ms = 0;
};

struct ApprovalNonce {
    std::string value;
    NonceState state = NonceState::Unburned;
    std::optional<device::Seq> burned_at_seq;
};

struct InitiatorProvenance {
    gates::InitiatorKind kind = gates::InitiatorKind::Human;
    std::string identity;
    std::string seat;
    std::optional<std::string> agent_run_id;
    device::Seq at_seq = 0;
};

struct ApprovalAttendance {
    AttendanceMode mode = AttendanceMode::PhysicalAttended;
    std::string attested_by;
    device::Seq at_seq = 0;
};

struct ApprovalBinding {
    std::string approval_id;
    std::string run_id;
    device::Seq issued_at_seq = 0;
    ApprovalMachine machine;
    ApprovalArtifact artifact;
    ApprovalJob job;
    ApprovalSnapshot snapshot;
    ApprovalPreflight preflight;
    Approver approver;
    ApprovalExpiry expiry;
    ExercisableBy exercisable_by = ExercisableBy::HumanOnly;
    ApprovalNonce nonce;
    uint32_t max_starts = 1;
    std::optional<InitiatorProvenance> initiator;
    ApprovalAttendance attendance;
    SpindleAuthority spindle_authority = SpindleAuthority::NotApplicable;
    uint64_t policy_revision = 0;
};

struct ApprovalInputs {
    ApprovalMachine machine;
    ApprovalArtifact artifact;
    ApprovalJob job;
    ApprovalSnapshot snapshot;
    ApprovalPreflight preflight;
    uint64_t policy_revision = 0;
    bool attendance_attestation_active = true;
};

struct Falsification {
    std::string reason;
    std::string why;
};

std::optional<Falsification> falsification_against(const ApprovalBinding& binding,
                                                   const ApprovalInputs& current,
                                                   uint64_t now_mono_ns);

struct NonceBurnResult {
    bool burned = false;
    std::string code;
    std::string why;
    std::optional<device::Seq> burned_at_seq;
};

class NonceLedger {
public:
    bool register_approval(const ApprovalBinding& binding, std::string* why = nullptr);
    NonceBurnResult burn(std::string_view approval_id, std::string_view expected_nonce,
                         device::Seq at_seq);
    NonceBurnResult burn_durably(std::string_view approval_id,
                                 std::string_view expected_nonce,
                                 const std::function<device::Seq()>& durable_append);
    std::optional<ApprovalNonce> describe(std::string_view approval_id) const;

private:
    mutable std::mutex mutex_;
    std::map<std::string, ApprovalNonce, std::less<>> rows_;
};

}  // namespace chudware::mfg
