#include "manufacturing/gates.h"

#include <algorithm>
#include <stdexcept>

namespace chudware::mfg::gates {
namespace {

template <typename T>
bool contains(const std::vector<T>& values, const T& value) {
    return std::find(values.begin(), values.end(), value) != values.end();
}

bool is_subject_namespace_bound_for_fact(std::string_view value) {
    return value == "art" || value == "job" || value == "res";
}

GuardResult pass(Guard guard) { return {guard, GuardVerdict::Pass, {}}; }
GuardResult fail(Guard guard, std::string why) {
    return {guard, GuardVerdict::Fail, std::move(why)};
}

}  // namespace

std::string_view to_string(Admission value) {
    switch (value) {
        case Admission::ProbedEvidence: return "probed_evidence";
        case Admission::ProceduralFact: return "procedural_fact";
    }
    throw std::invalid_argument("invalid Admission");
}

std::string_view to_string(GuardVerdict value) {
    switch (value) {
        case GuardVerdict::Pass: return "pass";
        case GuardVerdict::Fail: return "fail";
        case GuardVerdict::NotApplicable: return "not_applicable";
    }
    throw std::invalid_argument("invalid GuardVerdict");
}

std::string_view to_string(Guard value) {
    switch (value) {
        case Guard::ArtifactHashMatchesApproval: return "artifact_hash_matches_approval";
        case Guard::MachineFingerprintMatches: return "machine_fingerprint_matches";
        case Guard::EnvelopeWithinTravel: return "envelope_within_travel";
        case Guard::WcsEstablishedThisSetup: return "wcs_established_this_setup";
        case Guard::AttendancePolicySatisfied: return "attendance_policy_satisfied";
        case Guard::SpindleAuthoritySatisfied: return "spindle_authority_satisfied";
        case Guard::AlarmHistoryClearSincePreflight: return "alarm_history_clear_since_preflight";
        case Guard::NonceUnburned: return "nonce_unburned";
        case Guard::ExercisableByPermitsInitiator: return "exercisable_by_permits_initiator";
        case Guard::ApprovalUnexpiredAtSeq: return "approval_unexpired_at_seq";
        case Guard::EmulatorStageAtLeast: return "emulator_stage_at_least";
        case Guard::InterlockEngaged: return "interlock_engaged";
        case Guard::FeedholdAckObserved: return "feedhold_ack_observed";
        case Guard::TierAtLeastQ2: return "tier_at_least_q2";
        case Guard::SlicerProfileHashMatches: return "slicer_profile_hash_matches";
    }
    throw std::invalid_argument("invalid Guard");
}

GuardAccepts accepts_for_guard(Guard guard) {
    switch (guard) {
        case Guard::ArtifactHashMatchesApproval:
        case Guard::EnvelopeWithinTravel:
        case Guard::SlicerProfileHashMatches:
            return {{"art", "job", "res"}, {Admission::ProbedEvidence, Admission::ProceduralFact}};
        case Guard::TierAtLeastQ2:
            return {{"res"}, {Admission::ProbedEvidence}};
        case Guard::MachineFingerprintMatches:
        case Guard::WcsEstablishedThisSetup:
        case Guard::AttendancePolicySatisfied:
        case Guard::SpindleAuthoritySatisfied:
        case Guard::AlarmHistoryClearSincePreflight:
        case Guard::NonceUnburned:
        case Guard::ExercisableByPermitsInitiator:
        case Guard::ApprovalUnexpiredAtSeq:
        case Guard::EmulatorStageAtLeast:
        case Guard::InterlockEngaged:
        case Guard::FeedholdAckObserved:
            return {{"dev", "run", "res"}, {Admission::ProbedEvidence}};
    }
    throw std::invalid_argument("invalid Guard");
}

AdmitResult admit(const AdmissionCandidate& candidate, const SubjectRef& subject,
                  const GuardAccepts& accepts) {
    AdmitResult result{Admit::Rejected, candidate.offered_as, {}};
    if (!contains(accepts.subject_ns, subject.subject_ns)) {
        result.why = "subject_namespace_not_accepted:" + subject.subject_ns;
        return result;
    }
    if (!contains(accepts.admission, candidate.offered_as)) {
        result.why = "admission_class_not_accepted:" + std::string(to_string(candidate.offered_as));
        return result;
    }

    if (candidate.offered_as == Admission::ProceduralFact) {
        if (subject.execute_side || subject.approval_bound_device_guard) {
            result.why = "procedural_fact_forbidden_for_execute_side_subject";
            return result;
        }
        if (!is_subject_namespace_bound_for_fact(subject.subject_ns)) {
            result.why = "procedural_fact_subject_must_be_art_job_or_res";
            return result;
        }
        if (!candidate.procedural_fact) {
            result.why = "procedural_fact_payload_missing";
            return result;
        }
        const auto& fact = *candidate.procedural_fact;
        if (fact.bound_subject != subject.id || fact.config_epoch != subject.config_epoch) {
            result.why = "procedural_fact_binding_mismatch";
            return result;
        }
        if (fact.check_name.empty() || fact.artifact_hash.empty() || fact.at_seq == 0 ||
            fact.outcome.empty()) {
            result.why = "procedural_fact_incomplete";
            return result;
        }
        result.admit = Admit::Admitted;
        return result;
    }

    const auto& fresh = candidate.fresh;
    if (fresh.state != FreshState::Ok) {
        result.why = "state=" + std::string(mfg::to_string(fresh.state)) +
                     (fresh.why.empty() ? std::string{} : ":" + fresh.why);
        return result;
    }
    if (!fresh.has_value) {
        result.why = "state=ok_but_value_missing";
        return result;
    }
    if (fresh.ttl_ms && fresh.age_ms > *fresh.ttl_ms) {
        result.why = "freshness_ttl_exceeded";
        return result;
    }

    const bool own_emulated_device =
        fresh.src == FreshSource::Simulated &&
        subject.machine_class == device::MachineClass::Emulated &&
        !subject.device_id.empty() && subject.device_id == candidate.approval_device_id;
    if (fresh.src != FreshSource::Probed && !own_emulated_device) {
        result.why = "source=" + std::string(mfg::to_string(fresh.src)) + "_not_probed";
        return result;
    }
    if ((subject.execute_side || subject.approval_bound_device_guard) &&
        candidate.execute_truth != device::CapabilityTruthValue::Verified) {
        result.why = "capability.execute=" +
                     std::string(device::to_string(candidate.execute_truth)) + "_not_verified";
        return result;
    }
    result.admit = Admit::Admitted;
    return result;
}

CorroborationResult corroborate(const CorroborationEvidence& claim,
                                const CorroborationEvidence& candidate) {
    if (claim.subject != candidate.subject || claim.config_epoch != candidate.config_epoch) {
        return {false, "binding", "corroborator must bind the same subject and config epoch"};
    }
    if (!candidate.admitted_fresh) {
        return {false, "freshness", "corroborator did not pass Stage-1 admission"};
    }
    if (claim.source_id == candidate.source_id || claim.lane_id == candidate.lane_id) {
        return {false, "independence", "corroborator must use a different source and lane"};
    }
    return {true, {}, {}};
}

GuardResult evaluate_guard(Guard guard, const GuardFacts& facts) {
    switch (guard) {
        case Guard::ArtifactHashMatchesApproval:
            return facts.artifact_hash == facts.approved_artifact_hash && !facts.artifact_hash.empty()
                       ? pass(guard) : fail(guard, "artifact hash differs from frozen approval");
        case Guard::MachineFingerprintMatches:
            return facts.machine_fingerprint == facts.approved_machine_fingerprint &&
                           !facts.machine_fingerprint.empty()
                       ? pass(guard) : fail(guard, "machine fingerprint differs from frozen approval");
        case Guard::EnvelopeWithinTravel:
            for (size_t axis = 0; axis < facts.envelope_min.size(); ++axis)
                if (facts.envelope_min[axis] < facts.travel_min[axis] ||
                    facts.envelope_max[axis] > facts.travel_max[axis] ||
                    facts.envelope_min[axis] > facts.envelope_max[axis])
                    return fail(guard, "artifact envelope exceeds commissioned travel");
            return pass(guard);
        case Guard::WcsEstablishedThisSetup:
            return facts.wcs_evidence_seq >= facts.setup_revision_seq && facts.wcs_evidence_seq != 0
                       ? pass(guard) : fail(guard, "WCS evidence predates the setup revision");
        case Guard::AttendancePolicySatisfied:
            return facts.attendance_policy_satisfied ? pass(guard)
                : fail(guard, "attendance attestation is not allowed by machine-class policy");
        case Guard::SpindleAuthoritySatisfied:
            return facts.spindle_authority_satisfied ? pass(guard)
                : fail(guard, "spindle authority is not confirmed for this run");
        case Guard::AlarmHistoryClearSincePreflight:
            return !facts.alarm_after_preflight ? pass(guard)
                : fail(guard, "alarm was journaled after preflight");
        case Guard::NonceUnburned:
            return facts.nonce_unburned ? pass(guard) : fail(guard, "approval nonce is not unburned");
        case Guard::ExercisableByPermitsInitiator:
            return facts.initiator_kind == InitiatorKind::Human ||
                           facts.exercisable_by == ExercisableBy::HumanOrAgent
                       ? pass(guard) : fail(guard, "human_only approval refuses agent initiator");
        case Guard::ApprovalUnexpiredAtSeq:
            return facts.now_mono_ns < facts.approval_expiry_mono_ns
                       ? pass(guard) : fail(guard, "approval expired before revalidation seq");
        case Guard::EmulatorStageAtLeast:
            return static_cast<int>(facts.emulator_stage_passed) >=
                           static_cast<int>(facts.emulator_stage_required)
                       ? pass(guard) : fail(guard, "emulator qualification stage is below the required ceiling");
        case Guard::InterlockEngaged:
            if (facts.interlock_closed) return pass(guard);
            if (facts.missing_interlock_claim && facts.missing_interlock_corroborator) {
                const auto result = corroborate(*facts.missing_interlock_claim,
                                                *facts.missing_interlock_corroborator);
                if (result.corroborated)
                    return {guard, GuardVerdict::NotApplicable,
                            "missing interlock sub-condition independently corroborated"};
                return fail(guard, "interlock missing and corroboration rejected on " +
                                   result.rejecting_axis + " axis");
            }
            return fail(guard, "interlock is not engaged");
        case Guard::FeedholdAckObserved:
            return facts.feedhold_ack_hold0_observed ? pass(guard)
                : fail(guard, "Hold:0 acknowledgement has not been observed");
        case Guard::TierAtLeastQ2:
            return facts.metrology_tier >= 2 ? pass(guard)
                : fail(guard, "bound metrology evidence is below Q2");
        case Guard::SlicerProfileHashMatches:
            return facts.slicer_profile_hash == facts.approved_slicer_profile_hash &&
                           !facts.slicer_profile_hash.empty()
                       ? pass(guard) : fail(guard, "slicer profile hash differs from frozen approval");
    }
    throw std::invalid_argument("invalid Guard");
}

std::vector<Guard> required_guards(device::MachineClass machine_class,
                                   tier0::ArtifactKind artifact_kind,
                                   bool offset_bound) {
    std::vector<Guard> result = {
        Guard::ArtifactHashMatchesApproval, Guard::MachineFingerprintMatches,
        Guard::EnvelopeWithinTravel, Guard::AttendancePolicySatisfied,
        Guard::AlarmHistoryClearSincePreflight, Guard::NonceUnburned,
        Guard::ExercisableByPermitsInitiator, Guard::ApprovalUnexpiredAtSeq,
        Guard::EmulatorStageAtLeast};

    const bool router_or_mill = machine_class == device::MachineClass::OpenFrameRouter ||
        machine_class == device::MachineClass::OpenFrameMill ||
        machine_class == device::MachineClass::EnclosedInterlockedMill;
    if (router_or_mill) {
        result.push_back(Guard::WcsEstablishedThisSetup);
        result.push_back(Guard::SpindleAuthoritySatisfied);
        result.push_back(Guard::FeedholdAckObserved);
    } else if (machine_class == device::MachineClass::Emulated) {
        result.push_back(Guard::FeedholdAckObserved);
    }
    if (machine_class == device::MachineClass::EnclosedInterlockedMill)
        result.push_back(Guard::InterlockEngaged);
    if (artifact_kind == tier0::ArtifactKind::PrintJob)
        result.push_back(Guard::SlicerProfileHashMatches);
    if (offset_bound) result.push_back(Guard::TierAtLeastQ2);
    return result;
}

GateReport evaluate(const GateInput& input, std::string subject, std::string result_ref,
                    device::Seq establishing_seq) {
    GateReport report;
    report.ref = std::move(result_ref);
    report.seq = establishing_seq;
    report.subject = std::move(subject);
    report.required_set = required_guards(input.machine_class, input.artifact_kind,
                                          input.offset_bound);
    report.waivers = input.waivers;
    if (input.stage1.empty()) {
        AdmitResult missing{Admit::Rejected, Admission::ProbedEvidence,
                            "required_stage1_evidence_missing"};
        report.stage1.push_back({"required_evidence", missing});
        report.rejecting.push_back("stage1:required_evidence:required_stage1_evidence_missing");
    }

    for (const auto& check : input.stage1) {
        auto result = admit(check.candidate, check.subject, check.accepts);
        if (result.admit == Admit::Rejected)
            report.rejecting.push_back("stage1:" + check.name + ":" + result.why);
        report.stage1.push_back({check.name, std::move(result)});
    }

    for (const auto guard : report.required_set) {
        auto result = evaluate_guard(guard, input.facts);
        const auto waiver = std::find_if(input.waivers.begin(), input.waivers.end(),
            [&](const Waiver& value) {
                return value.guard == guard && !value.by.empty() && !value.reason.empty() &&
                       value.at_seq != 0 && value.at_seq <= establishing_seq;
            });
        const bool valid_waiver = waiver != input.waivers.end();
        if (result.verdict == GuardVerdict::NotApplicable ||
            (result.verdict == GuardVerdict::Fail && !valid_waiver)) {
            report.rejecting.push_back("stage2:" + std::string(to_string(guard)) + ":" + result.why);
        }
        report.stage2.push_back(std::move(result));
    }
    report.passed = report.rejecting.empty();
    return report;
}

}  // namespace chudware::mfg::gates
