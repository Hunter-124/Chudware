#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "device/capability.h"
#include "device/device_id.h"
#include "manufacturing/fresh_value.h"
#include "manufacturing/policy.h"
#include "manufacturing/tier0.h"

namespace chudware::mfg::gates {

enum class Admit { Admitted, Rejected };
enum class Admission { ProbedEvidence, ProceduralFact };
enum class GuardVerdict { Pass, Fail, NotApplicable };

enum class Guard {
    ArtifactHashMatchesApproval,
    MachineFingerprintMatches,
    EnvelopeWithinTravel,
    WcsEstablishedThisSetup,
    AttendancePolicySatisfied,
    SpindleAuthoritySatisfied,
    AlarmHistoryClearSincePreflight,
    NonceUnburned,
    ExercisableByPermitsInitiator,
    ApprovalUnexpiredAtSeq,
    EmulatorStageAtLeast,
    InterlockEngaged,
    FeedholdAckObserved,
    TierAtLeastQ2,
    SlicerProfileHashMatches,
};

inline constexpr std::array<Guard, 15> all_guards = {
    Guard::ArtifactHashMatchesApproval, Guard::MachineFingerprintMatches,
    Guard::EnvelopeWithinTravel, Guard::WcsEstablishedThisSetup,
    Guard::AttendancePolicySatisfied, Guard::SpindleAuthoritySatisfied,
    Guard::AlarmHistoryClearSincePreflight, Guard::NonceUnburned,
    Guard::ExercisableByPermitsInitiator, Guard::ApprovalUnexpiredAtSeq,
    Guard::EmulatorStageAtLeast, Guard::InterlockEngaged,
    Guard::FeedholdAckObserved, Guard::TierAtLeastQ2,
    Guard::SlicerProfileHashMatches};

std::string_view to_string(Admission value);
std::string_view to_string(GuardVerdict value);
std::string_view to_string(Guard value);

struct SubjectRef {
    std::string id;
    std::string subject_ns;
    uint64_t config_epoch = 0;
    bool execute_side = false;
    bool approval_bound_device_guard = false;
    std::string device_id;
    device::MachineClass machine_class = device::MachineClass::Emulated;
};

struct GuardAccepts {
    std::vector<std::string> subject_ns;
    std::vector<Admission> admission;
};

struct FreshValueMeta {
    FreshState state = FreshState::Unknown;
    FreshSource src = FreshSource::Declared;
    bool has_value = false;
    uint64_t age_ms = 0;
    std::optional<uint64_t> ttl_ms;
    device::Seq seq = 0;
    std::string source_id;
    std::string lane_id;
    std::string why;
};

template <typename T>
FreshValueMeta metadata(const FreshValue<T>& value, std::string source_id,
                        std::string lane_id) {
    return {value.state, value.src, value.v.has_value(), value.age_ms, value.ttl_ms,
            value.seq, std::move(source_id), std::move(lane_id), value.why};
}

struct ProceduralFact {
    std::string check_name;
    std::string bound_subject;
    std::string artifact_hash;
    uint64_t config_epoch = 0;
    std::optional<std::string> snapshot_id;
    device::Seq at_seq = 0;
    std::string outcome;
};

struct AdmissionCandidate {
    Admission offered_as = Admission::ProbedEvidence;
    FreshValueMeta fresh;
    std::optional<ProceduralFact> procedural_fact;
    device::CapabilityTruthValue execute_truth = device::CapabilityTruthValue::Unsupported;
    std::string approval_device_id;
};

struct AdmitResult {
    Admit admit = Admit::Rejected;
    Admission cls = Admission::ProbedEvidence;
    std::string why;
};

AdmitResult admit(const AdmissionCandidate& candidate, const SubjectRef& subject,
                  const GuardAccepts& accepts);
GuardAccepts accepts_for_guard(Guard guard);

struct CorroborationEvidence {
    std::string subject;
    uint64_t config_epoch = 0;
    std::string source_id;
    std::string lane_id;
    bool admitted_fresh = false;
};

struct CorroborationResult {
    bool corroborated = false;
    std::string rejecting_axis;
    std::string why;
};

CorroborationResult corroborate(const CorroborationEvidence& claim,
                                const CorroborationEvidence& candidate);

enum class InitiatorKind { Human, Agent };

struct GuardFacts {
    std::string artifact_hash;
    std::string approved_artifact_hash;
    std::string machine_fingerprint;
    std::string approved_machine_fingerprint;
    std::array<double, 3> envelope_min{};
    std::array<double, 3> envelope_max{};
    std::array<double, 3> travel_min{};
    std::array<double, 3> travel_max{};
    device::Seq wcs_evidence_seq = 0;
    device::Seq setup_revision_seq = 0;
    bool attendance_policy_satisfied = false;
    bool spindle_authority_satisfied = false;
    bool alarm_after_preflight = true;
    bool nonce_unburned = false;
    ExercisableBy exercisable_by = ExercisableBy::HumanOnly;
    InitiatorKind initiator_kind = InitiatorKind::Human;
    uint64_t now_mono_ns = 0;
    uint64_t approval_expiry_mono_ns = 0;
    device::EmulatorStage emulator_stage_passed = device::EmulatorStage::None;
    device::EmulatorStage emulator_stage_required = device::EmulatorStage::E6;
    bool interlock_closed = false;
    std::optional<CorroborationEvidence> missing_interlock_claim;
    std::optional<CorroborationEvidence> missing_interlock_corroborator;
    bool feedhold_ack_hold0_observed = false;
    int metrology_tier = 0;
    std::string slicer_profile_hash;
    std::string approved_slicer_profile_hash;
};

struct GuardResult {
    Guard guard = Guard::ArtifactHashMatchesApproval;
    GuardVerdict verdict = GuardVerdict::Fail;
    std::string why;
};

GuardResult evaluate_guard(Guard guard, const GuardFacts& facts);
std::vector<Guard> required_guards(device::MachineClass machine_class,
                                   tier0::ArtifactKind artifact_kind,
                                   bool offset_bound);

struct Waiver {
    Guard guard = Guard::ArtifactHashMatchesApproval;
    std::string by;
    std::string reason;
    device::Seq at_seq = 0;
};

struct Stage1Check {
    std::string name;
    SubjectRef subject;
    AdmissionCandidate candidate;
    GuardAccepts accepts;
};

struct Stage1Report {
    std::string name;
    AdmitResult result;
};

struct GateInput {
    std::vector<Stage1Check> stage1;
    GuardFacts facts;
    device::MachineClass machine_class = device::MachineClass::Emulated;
    tier0::ArtifactKind artifact_kind = tier0::ArtifactKind::Nc;
    bool offset_bound = false;
    std::vector<Waiver> waivers;
};

struct GateReport {
    std::string ref;
    device::Seq seq = 0;
    std::string subject;
    std::vector<Stage1Report> stage1;
    std::vector<GuardResult> stage2;
    std::vector<Guard> required_set;
    std::vector<Waiver> waivers;
    bool passed = false;
    std::vector<std::string> rejecting;
};

GateReport evaluate(const GateInput& input, std::string subject, std::string result_ref,
                    device::Seq establishing_seq);

}  // namespace chudware::mfg::gates
