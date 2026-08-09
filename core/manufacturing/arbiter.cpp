#include "manufacturing/arbiter.h"

#include <stdexcept>

namespace chudware::mfg {
namespace {

std::string_view hold_kind_name(HoldKind value) {
    switch (value) {
        case HoldKind::FeedHold: return "feed_hold";
        case HoldKind::ControlledStop: return "controlled_stop";
        case HoldKind::DoorSuspend: return "door_suspend";
    }
    throw std::invalid_argument("invalid HoldKind");
}

std::string_view initiator_kind_name(gates::InitiatorKind value) {
    return value == gates::InitiatorKind::Human ? "human" : "agent";
}

std::string_view discontinuity_name(device::RunDiscontinuity value) {
    switch (value) {
        case device::RunDiscontinuity::None: return "none";
        case device::RunDiscontinuity::Reset: return "reset";
        case device::RunDiscontinuity::Alarm: return "alarm";
        case device::RunDiscontinuity::HardLimit: return "hard_limit";
        case device::RunDiscontinuity::Disconnect: return "disconnect";
        case device::RunDiscontinuity::PowerLoss: return "power_loss";
    }
    throw std::invalid_argument("invalid RunDiscontinuity");
}

nlohmann::json initiator_json(const Initiator& initiator) {
    nlohmann::json value = {{"kind", initiator_kind_name(initiator.kind)},
                            {"identity", initiator.identity}, {"seat", initiator.seat}};
    if (initiator.agent_run_id) value["agent_run_id"] = *initiator.agent_run_id;
    return value;
}

}  // namespace

bool Arbiter::register_approval(ApprovalBinding approval, std::string* why) {
    std::lock_guard lock(mutex_);
    if (approval.approval_id.empty() || approval.run_id.empty()) {
        if (why) *why = "approval_id and run_id are required";
        return false;
    }
    if (approval.approver.identity.empty() || approval.approver.auth.empty()) {
        if (why) *why = "approval requires an authenticated human approver";
        return false;
    }
    if (approval.max_starts != 1 || approval.expiry.t_mono_ns <= approval.approver.at_mono_ns) {
        if (why) *why = "approval requires max_starts=1 and a future expiry";
        return false;
    }
    if (approvals_.contains(approval.approval_id)) {
        if (why) *why = "approval already registered";
        return false;
    }
    if (!nonces_.register_approval(approval, why)) return false;
    journal_.append("mfg.approval.issued", approval.approval_id,
                    {{"run_id", approval.run_id}, {"approver", approval.approver.identity},
                     {"exercisable_by", mfg::to_string(approval.exercisable_by)},
                     {"max_starts", approval.max_starts}});
    approvals_.emplace(approval.approval_id, std::move(approval));
    return true;
}

bool Arbiter::register_run(RunAuthorization run, std::string* why) {
    std::lock_guard lock(mutex_);
    if (run.run_id.empty() || run.approval_id.empty()) {
        if (why) *why = "run_id and approval_id are required";
        return false;
    }
    const auto approval = approvals_.find(run.approval_id);
    if (approval == approvals_.end() || approval->second.run_id != run.run_id) {
        if (why) *why = "run must name its frozen approval and approval-bound run_id";
        return false;
    }
    if (run.state != device::RunState::Approved) {
        if (why) *why = "registered run must begin in approved state";
        return false;
    }
    if (runs_.contains(run.run_id)) {
        if (why) *why = "run already registered";
        return false;
    }
    runs_.emplace(run.run_id, std::move(run));
    return true;
}

bool Arbiter::replace_current_inputs(std::string_view run_id, ApprovalInputs current,
                                     std::string* why) {
    std::lock_guard lock(mutex_);
    const auto it = runs_.find(run_id);
    if (it == runs_.end()) {
        if (why) *why = "run not found";
        return false;
    }
    it->second.current_inputs = std::move(current);
    return true;
}

tier0::Context Arbiter::materialize_tier0(const RunAuthorization& run,
                                          const ApprovalBinding& approval) const {
    auto context = run.tier0_context;
    const auto nonce = nonces_.describe(approval.approval_id);
    context.approval_present = true;
    context.approval_unexpired = clock_.mono_ns() < approval.expiry.t_mono_ns;
    context.approval_unburned = nonce && nonce->state == NonceState::Unburned;
    context.attendance_mode = approval.attendance.mode;
    context.attendance_attested = !approval.attendance.attested_by.empty() &&
                                  run.current_inputs.attendance_attestation_active;
    context.requested_exercisable_by = approval.exercisable_by;
    context.policy.machine_class = context.machine_class;
    context.emulator_stage_passed = run.gate_input.facts.emulator_stage_passed;
    switch (approval.spindle_authority) {
        case SpindleAuthority::NotApplicable:
            context.spindle_request = SpindleRequest::NotApplicable;
            break;
        case SpindleAuthority::ManualConfirmed:
            context.spindle_request = SpindleRequest::ManualMotion;
            context.manual_spindle_confirmation = true;
            break;
        case SpindleAuthority::CommandedFeedback:
            context.spindle_request = SpindleRequest::AutomatedControl;
            break;
    }
    return context;
}

gates::GateInput Arbiter::materialize_gate_input(const RunAuthorization& run,
                                                 const ApprovalBinding& approval,
                                                 const Initiator& initiator) const {
    auto input = run.gate_input;
    auto& facts = input.facts;
    facts.artifact_hash = run.current_inputs.artifact.hash_sha256;
    facts.approved_artifact_hash = approval.artifact.hash_sha256;
    facts.machine_fingerprint = run.current_inputs.machine.fingerprint_sha256;
    facts.approved_machine_fingerprint = approval.machine.fingerprint_sha256;
    facts.exercisable_by = approval.exercisable_by;
    facts.initiator_kind = initiator.kind;
    facts.now_mono_ns = clock_.mono_ns();
    facts.approval_expiry_mono_ns = approval.expiry.t_mono_ns;
    facts.nonce_unburned = nonces_.describe(approval.approval_id).has_value() &&
        nonces_.describe(approval.approval_id)->state == NonceState::Unburned;
    facts.emulator_stage_required = input.machine_class == device::MachineClass::Emulated
        ? device::EmulatorStage::E3 : device::EmulatorStage::E6;
    facts.slicer_profile_hash = run.current_inputs.artifact.slicer_profile_hash;
    facts.approved_slicer_profile_hash = approval.artifact.slicer_profile_hash;

    const auto attendance = check_attendance(run.tier0_context.policy,
                                             approval.attendance.mode,
                                             !approval.attendance.attested_by.empty() &&
                                                 run.current_inputs.attendance_attestation_active);
    facts.attendance_policy_satisfied = attendance.allowed;
    const auto tier0_context = materialize_tier0(run, approval);
    facts.spindle_authority_satisfied = check_spindle_authority(
        tier0_context.policy, tier0_context.spindle_request,
        tier0_context.manual_spindle_confirmation, tier0_context.spindle_commanded_state,
        tier0_context.spindle_observed_state).allowed;
    return input;
}

device::Seq Arbiter::journal_attempt(std::string_view action, std::string_view run_id,
                                     const Initiator& initiator, std::string_view outcome,
                                     std::string_view reason) {
    return journal_.append("mfg.arbiter.attempt", run_id,
                           {{"action", action}, {"initiator", initiator_json(initiator)},
                            {"outcome", outcome}, {"reason", reason}});
}

bool Arbiter::transition(RunAuthorization& run, device::RunState to,
                         std::string_view cause, device::Seq* at_seq) {
    const auto validation = device::validate_run_transition(
        run.state, to, to == device::RunState::Resuming
                           ? run.discontinuity_since_hold : device::RunDiscontinuity::None);
    if (!validation.allowed) return false;
    const auto from = run.state;
    run.state = to;
    const auto seq = journal_.append("mfg.run.state", run.run_id,
        {{"from", device::to_string(from)}, {"state", device::to_string(to)}, {"cause", cause}});
    if (at_seq) *at_seq = seq;
    return true;
}

std::optional<gates::GateReport> Arbiter::evaluate(std::string_view run_id,
                                                   const Initiator& initiator) const {
    std::lock_guard lock(mutex_);
    const auto run = runs_.find(run_id);
    if (run == runs_.end()) return std::nullopt;
    const auto approval = approvals_.find(run->second.approval_id);
    if (approval == approvals_.end()) return std::nullopt;
    return gates::evaluate(materialize_gate_input(run->second, approval->second, initiator),
                           run->second.run_id, {}, 0);
}

StartOutcome Arbiter::start(std::string_view run_id, std::string_view approval_id,
                            const Initiator& initiator) {
    std::lock_guard lock(mutex_);
    auto run_it = runs_.find(run_id);
    if (run_it == runs_.end()) {
        return {false, "run.not_found", "run is not registered", device::RunState::Draft,
                std::nullopt, std::nullopt, std::nullopt};
    }
    auto approval_it = approvals_.find(approval_id);
    if (approval_it == approvals_.end() || run_it->second.approval_id != approval_id ||
        approval_it->second.run_id != run_id) {
        journal_attempt("start", run_id, initiator, "refused", "approval.binding_to_run_mismatch");
        return {false, "approval.binding_to_run_mismatch",
                "approval is not bound to this run", run_it->second.state,
                std::nullopt, std::nullopt, std::nullopt};
    }
    auto& run = run_it->second;
    auto& approval = approval_it->second;
    journal_attempt("start", run_id, initiator, "attempted");

    const auto tier0_context = materialize_tier0(run, approval);
    const auto construction_refusals = tier0::evaluate(tier0_context);
    if (!construction_refusals.empty()) {
        const auto& refusal = construction_refusals.front();
        journal_attempt("start", run_id, initiator, "refused", refusal.prerequisite);
        return {false, "tier0:" + refusal.prerequisite, refusal.why, run.state,
                std::nullopt, std::nullopt, std::nullopt};
    }
    if (run.state != device::RunState::Approved) {
        journal_attempt("start", run_id, initiator, "refused", "run.state_not_approved");
        return {false, "run.state_not_approved", "only an approved authored run may start",
                run.state, std::nullopt, std::nullopt, std::nullopt};
    }

    const auto report_ref = device::ResultRef::mint("gate_report").str();
    const auto expected_seq = journal_.head() + 1;
    auto gate_report = gates::evaluate(materialize_gate_input(run, approval, initiator),
                                       run.run_id, report_ref, expected_seq);
    nlohmann::json stage1 = nlohmann::json::array();
    for (const auto& check : gate_report.stage1) {
        stage1.push_back({
            {"name", check.name},
            {"admission", gates::to_string(check.result.cls)},
            {"admit", check.result.admit == gates::Admit::Admitted ? "admitted" : "rejected"},
            {"why", check.result.why}});
    }
    nlohmann::json stage2 = nlohmann::json::array();
    for (const auto& result : gate_report.stage2)
        stage2.push_back({{"guard", gates::to_string(result.guard)},
                          {"verdict", gates::to_string(result.verdict)}, {"why", result.why}});
    nlohmann::json required_set = nlohmann::json::array();
    for (const auto guard : gate_report.required_set)
        required_set.push_back(gates::to_string(guard));
    nlohmann::json waivers = nlohmann::json::array();
    for (const auto& waiver : gate_report.waivers) {
        waivers.push_back({{"guard", gates::to_string(waiver.guard)}, {"by", waiver.by},
                           {"reason", waiver.reason}, {"at_seq", waiver.at_seq}});
    }
    const auto gate_seq = journal_.append("mfg.run.gate_report", run.run_id,
        {{"ref", report_ref}, {"subject", gate_report.subject},
         {"passed", gate_report.passed}, {"rejecting", gate_report.rejecting},
         {"stage1", std::move(stage1)}, {"stage2", std::move(stage2)},
         {"required_set", std::move(required_set)}, {"waivers", std::move(waivers)}});
    gate_report.seq = gate_seq;
    run.gate_report_ref = report_ref;
    if (!gate_report.passed) {
        if (const auto falsification = falsification_against(approval, run.current_inputs,
                                                             clock_.mono_ns())) {
            journal_.append("mfg.result.falsified", approval.approval_id,
                            {{"class", "falsified"}, {"reason", falsification->reason},
                             {"why", falsification->why},
                             {"affected", nlohmann::json::array({approval.approval_id, run.run_id})}});
        }
        const auto reason = gate_report.rejecting.empty() ? "gate_report_incomplete"
                                                          : gate_report.rejecting.front();
        journal_attempt("start", run_id, initiator, "refused", reason);
        return {false, reason, "authorization gate report failed", run.state,
                gate_seq, std::nullopt, gate_report};
    }

    if (const auto falsification = falsification_against(approval, run.current_inputs,
                                                         clock_.mono_ns())) {
        journal_.append("mfg.result.falsified", approval.approval_id,
                        {{"class", "falsified"}, {"reason", falsification->reason},
                         {"why", falsification->why},
                         {"affected", nlohmann::json::array({approval.approval_id, run.run_id})}});
        journal_attempt("start", run_id, initiator, "refused", falsification->reason);
        return {false, "approval.falsified:" + falsification->reason,
                falsification->why, run.state, std::nullopt, std::nullopt, gate_report};
    }
    if (!permits(run.tier0_context.policy, approval.exercisable_by)) {
        journal_attempt("start", run_id, initiator, "refused", "exercisable_by_exceeds_policy");
        return {false, "policy:exercisable_by_exceeds_policy",
                "approval exceeds commissioning policy ceiling", run.state,
                std::nullopt, std::nullopt, gate_report};
    }

    auto burn = nonces_.burn_durably(
        approval.approval_id, approval.nonce.value, [&] {
            return journal_.append("mfg.approval.burned", approval.approval_id,
                {{"run_id", run.run_id}, {"initiator", initiator_json(initiator)},
                 {"nonce", approval.nonce.value}, {"max_starts", approval.max_starts}});
        });
    if (!burn.burned) {
        journal_attempt("start", run_id, initiator, "refused", burn.code);
        return {false, burn.code, burn.why, run.state, std::nullopt,
                burn.burned_at_seq, gate_report};
    }
    const auto burn_seq = *burn.burned_at_seq;
    approval.nonce.state = NonceState::Burned;
    approval.nonce.burned_at_seq = burn_seq;
    InitiatorProvenance provenance{initiator.kind, initiator.identity, initiator.seat,
                                   initiator.agent_run_id, burn_seq};
    approval.initiator = provenance;
    run.initiator = provenance;

    device::Seq start_seq = 0;
    if (!transition(run, device::RunState::Arming, "approval_revalidated", &start_seq) ||
        !transition(run, device::RunState::Queued, "device_armed", &start_seq) ||
        !transition(run, device::RunState::Running, "device_commanded", &start_seq)) {
        throw std::logic_error("canonical start transition failed after nonce burn");
    }
    journal_attempt("start", run_id, initiator, "accepted");
    return {true, {}, {}, run.state, start_seq, burn_seq, gate_report};
}

RunOutcome Arbiter::hold(std::string_view run_id, const Initiator& initiator, HoldKind kind) {
    std::lock_guard lock(mutex_);
    auto it = runs_.find(run_id);
    if (it == runs_.end())
        return {false, "run.not_found", "run is not registered",
                device::RunState::Draft, std::nullopt};
    auto& run = it->second;
    if (run.state != device::RunState::Running) {
        journal_attempt("hold", run_id, initiator, "refused", "run.not_running");
        return {false, "run.not_running", "hold requires a running run",
                run.state, std::nullopt};
    }
    device::Seq seq = 0;
    if (!transition(run, device::RunState::Holding, hold_kind_name(kind), &seq) ||
        !transition(run, device::RunState::Held, "hold_acknowledged", &seq))
        return {false, "run.transition_refused", "canonical hold transition refused",
                run.state, std::nullopt};
    journal_attempt("hold", run_id, initiator, "accepted", hold_kind_name(kind));
    return {true, {}, {}, run.state, seq};
}

RunOutcome Arbiter::abort(std::string_view run_id, const Initiator& initiator) {
    std::lock_guard lock(mutex_);
    auto it = runs_.find(run_id);
    if (it == runs_.end())
        return {false, "run.not_found", "run is not registered",
                device::RunState::Draft, std::nullopt};
    auto& run = it->second;
    if (run.state != device::RunState::Running && run.state != device::RunState::Held) {
        journal_attempt("abort", run_id, initiator, "refused", "run.not_abortable");
        return {false, "run.not_abortable", "abort requires a running or held run",
                run.state, std::nullopt};
    }
    device::Seq seq = 0;
    if (!transition(run, device::RunState::Aborting, "software_abort_requested", &seq))
        return {false, "run.transition_refused", "canonical abort transition refused",
                run.state, std::nullopt};
    journal_attempt("abort", run_id, initiator, "accepted");
    return {true, {}, {}, run.state, seq};
}

RunOutcome Arbiter::resume(std::string_view run_id, const Initiator& initiator) {
    std::lock_guard lock(mutex_);
    auto it = runs_.find(run_id);
    if (it == runs_.end())
        return {false, "run.not_found", "run is not registered",
                device::RunState::Draft, std::nullopt};
    auto& run = it->second;
    if (run.state != device::RunState::Held) {
        journal_attempt("resume", run_id, initiator, "refused", "run.not_held");
        return {false, "run.not_held", "resume requires a held run",
                run.state, std::nullopt};
    }
    if (run.discontinuity_since_hold != device::RunDiscontinuity::None) {
        const std::string code = run.discontinuity_since_hold == device::RunDiscontinuity::Alarm
            ? "no_alarm_since_hold" : "resume_forbidden_after_discontinuity";
        journal_attempt("resume", run_id, initiator, "refused", code);
        return {false, code,
                "transparent resume is forbidden after " +
                    std::string(discontinuity_name(run.discontinuity_since_hold)) +
                    "; author a new run",
                run.state, std::nullopt};
    }
    device::Seq seq = 0;
    if (!transition(run, device::RunState::Resuming, "operator_resume", &seq) ||
        !transition(run, device::RunState::Running, "resume_acknowledged", &seq))
        return {false, "run.transition_refused", "canonical resume transition refused",
                run.state, std::nullopt};
    journal_attempt("resume", run_id, initiator, "accepted");
    return {true, {}, {}, run.state, seq};
}

bool Arbiter::record_discontinuity(std::string_view run_id,
                                   device::RunDiscontinuity discontinuity) {
    std::lock_guard lock(mutex_);
    const auto it = runs_.find(run_id);
    if (it == runs_.end() || discontinuity == device::RunDiscontinuity::None) return false;
    it->second.discontinuity_since_hold = discontinuity;
    journal_.append("mfg.run.discontinuity", run_id,
                    {{"kind", discontinuity_name(discontinuity)},
                     {"transparent_resume", false}});
    return true;
}

std::optional<RunAuthorization> Arbiter::describe_run(std::string_view run_id) const {
    std::lock_guard lock(mutex_);
    const auto it = runs_.find(run_id);
    if (it == runs_.end()) return std::nullopt;
    return it->second;
}

std::optional<ApprovalBinding> Arbiter::describe_approval(std::string_view approval_id) const {
    std::lock_guard lock(mutex_);
    const auto it = approvals_.find(approval_id);
    if (it == approvals_.end()) return std::nullopt;
    return it->second;
}

}  // namespace chudware::mfg
