#include "hostd/mfg_lane.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace chudware::mfg {
namespace {

using nlohmann::json;

std::filesystem::path defaultStateDirectory() {
    if (const char* configured = std::getenv("CHUDWARE_MFG_STATE_DIR"); configured && *configured)
        return configured;
    if (const char* state = std::getenv("XDG_STATE_HOME"); state && *state)
        return std::filesystem::path(state) / "chudware" / "manufacturing";
    if (const char* home = std::getenv("HOME"); home && *home)
        return std::filesystem::path(home) / ".local" / "state" / "chudware" / "manufacturing";
    return std::filesystem::temp_directory_path() / "chudware-manufacturing";
}

device::ControllerIdentitySnapshot advertisedIdentity() {
    device::ControllerIdentitySnapshot identity;
    identity.firmware_banner = "Grbl 1.1h ['$' for help]";
    identity.build_options = "[OPT:VN,15,128];VER=chudware-emulator:1.0";
    identity.settings = {{"$0", "10"}, {"$1", "25"}, {"$100", "250.000"},
                         {"$101", "250.000"}, {"$102", "250.000"},
                         {"$130", "300.000"}, {"$131", "300.000"},
                         {"$132", "80.000"}};
    identity.axis_count = 3;
    identity.machine_class = device::MachineClass::Emulated;
    identity.transport_kind = "emulator";
    identity.transport_endpoint_id = "mfg-emulator";
    return identity;
}

device::GrblAdapterConfig adapterConfig() {
    device::GrblAdapterConfig config;
    config.machine_class = device::MachineClass::Emulated;
    config.freshness_ttl_ms = 600;
    return config;
}

json policyJson(const MachinePolicy& policy) {
    json attendance = json::array();
    for (const auto mode : policy.attendance_allowed) attendance.push_back(to_string(mode));
    return {{"machine_class", device::to_string(policy.machine_class)},
            {"attendance_allowed", std::move(attendance)},
            {"max_exercisable_by", to_string(policy.max_exercisable_by)},
            {"spindle_guard_required", policy.spindle_guard_required},
            {"feedback_relay_commissioned", policy.feedback_relay_commissioned},
            {"emulator_stage_passed", "E3"}, {"revision", policy.revision}};
}
MachinePolicy lanePolicy() {
    auto policy = default_policy(device::MachineClass::Emulated);
    policy.max_exercisable_by = ExercisableBy::HumanOrAgent;
    return policy;
}


json leaseJson(const CommandLease& lease) {
    return {{"lease_id", lease.lease_id}, {"device_id", lease.device_id},
            {"owner", {{"client_kind", lease.owner.client_kind}, {"seat", lease.owner.seat},
                       {"session", lease.owner.session}}},
            {"role", lease.role}, {"granted_at_seq", lease.granted_at_seq},
            {"ttl_ms", lease.ttl_ms}, {"heartbeat_ms", lease.heartbeat_ms},
            {"expires_at_mono_ns", lease.expires_at_mono_ns},
            {"transport_kind", lease.transport_kind},
            {"exclusive_endpoint", lease.exclusive_endpoint}};
}

std::vector<std::string> linesFromText(std::string_view text) {
    std::vector<std::string> lines;
    std::istringstream input{std::string(text)};
    for (std::string line; std::getline(input, line);) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (!line.empty()) lines.push_back(std::move(line));
    }
    return lines;
}

json initiatorJson(const InitiatorProvenance& value) {
    json out{{"kind", value.kind == gates::InitiatorKind::Human ? "human" : "agent"},
             {"identity", value.identity}, {"seat", value.seat}, {"at_seq", value.at_seq}};
    if (value.agent_run_id) out["agent_run_id"] = *value.agent_run_id;
    return out;
}

}  // namespace

uint64_t ManufacturingLane::SteadyClock::mono_ns() const {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count()) +
        offset_ns_.load();
}

ManufacturingLane::ManufacturingLane(std::filesystem::path state_directory,
                                     bool emulator_endpoint_is_physical)
    : epoch_(device::LaneEpoch::mint("manufacturing")),
      journal_((std::filesystem::create_directories(state_directory), std::move(state_directory)), epoch_),
      arbiter_(journal_, clock_, nonces_), leases_(journal_),
      result_registry_(ResultRegistry::manufacturingDefaults()),
      advertised_identity_(advertisedIdentity()),
      advertised_fingerprint_(device::MachineFingerprint::from_identity(advertised_identity_)),
      device_id_(device::DeviceId::mint(device::MachineClass::Emulated, advertised_fingerprint_)),
      endpoint_{"mfg-emulator", "inproc://mfg-emulator", emulator_endpoint_is_physical},
      emulator_(), adapter_(emulator_, [this] { return clock_.mono_ns(); }, adapterConfig(),
                            "manufacturing") {
    const auto seq = journal_.append("mfg.gateway.epoch", "manufacturing",
        {{"epoch", epoch_}, {"process_start_id", epoch_.process_start_id},
         {"epoch_floor_seq", journal_.epochFloor()},
         {"reconciled", {{"falsified", 0}, {"superseded", 0}, {"invariant", 0}}}});
    (void)seq;
}

ManufacturingLane::~ManufacturingLane() = default;

void ManufacturingLane::setEventSink(EventSink sink) {
    std::lock_guard lock(mutex_);
    event_sink_ = std::move(sink);
}

std::optional<LaneRpcResult> ManufacturingLane::handle(std::string_view method,
                                                        const json& params) {
    if (!method.starts_with("mfg_")) return std::nullopt;
    std::lock_guard lock(mutex_);
    try {
        if (!params.is_object()) return refuse("mfg_invalid_params", "params must be an object");
        return route(method, params);
    } catch (const std::invalid_argument& error) {
        return refuse("mfg_invalid_params", error.what());
    } catch (const json::exception& error) {
        return refuse("mfg_invalid_params", error.what());
    } catch (const std::exception& error) {
        return refuse("mfg_internal_error", error.what());
    }
}

LaneRpcResult ManufacturingLane::ok(json fields) const {
    if (!fields.is_object()) fields = json{{"value", std::move(fields)}};
    fields["result_seq"] = journal_.head();
    fields["epoch"] = epoch_;
    return {std::move(fields), false};
}

LaneRpcResult ManufacturingLane::refuse(std::string code, std::string message,
                                         json detail) const {
    json body{{"code", std::move(code)}, {"message", std::move(message)},
              {"result_seq", journal_.head()}, {"epoch", epoch_}};
    if (!detail.empty()) body["detail"] = std::move(detail);
    return {std::move(body), true};
}

bool ManufacturingLane::validDevice(const json& params, LaneRpcResult& refusal) const {
    const std::string requested = params.value("device_id", device_id_.str());
    if (requested == device_id_.str()) return true;
    refusal = refuse("mfg_device_not_found", "device_id is not owned by this lane",
                     {{"device_id", requested}});
    return false;
}

bool ManufacturingLane::requireLease(const json& params, LaneRpcResult& refusal) {
    if (!validDevice(params, refusal)) return false;
    const std::string lease_id = params.value("lease_id", std::string{});
    if (lease_id.empty()) {
        refusal = refuse("lease.commander_held_by_caller", "write requires a commander lease",
                         {{"prerequisite", "lease.commander_held_by_caller"}});
        return false;
    }
    const auto active = leases_.describe(device_id_.str(), clock_.mono_ns());
    if (!active || active->lease_id != lease_id) {
        refusal = refuse("lease.commander_held_by_caller",
                         "lease is absent, expired, or owned by another caller",
                         {{"prerequisite", "lease.commander_held_by_caller"},
                          {"lease_id", lease_id}});
        return false;
    }
    return true;
}

void ManufacturingLane::publishEvent(std::string_view kind, std::string_view subject,
                                      device::Seq seq, const json& body) {
    if (!event_sink_) return;
    json data = body;
    if (!data.is_object()) data = json{{"value", data}};
    data["seq"] = seq;
    data["at_seq"] = seq;
    data["subject"] = subject;
    event_sink_(json{{"event", kind}, {"data", std::move(data)}}.dump());
}

device::Seq ManufacturingLane::appendEvent(std::string kind, std::string subject,
                                            json body, json raw) {
    const auto seq = journal_.append(kind, subject, body, std::move(raw));
    publishEvent(kind, subject, seq, body);
    return seq;
}

void ManufacturingLane::serviceDevice() {
    if (!connected_) return;
    adapter_.service(journal_.head());
}

device::CapabilityReport ManufacturingLane::capabilityReport() const {
    device::CapabilityReport report;
    report.subject_id = device_id_.str();
    report.max_advertised = endpoint_.endpoint_is_physical
        ? device::CapabilityLevel::Transport : device::CapabilityLevel::Execute;
    report.emulator_stage_passed = device::EmulatorStage::E3;
    for (const auto level : device::capability_levels) {
        device::CapabilityTruth truth;
        truth.level = level;
        truth.emulator_stage = device::EmulatorStage::E3;
        if (level == device::CapabilityLevel::SafetyIntegration) {
            truth.truth = device::CapabilityTruthValue::Unsupported;
            truth.why = "emulated class has no interlock circuit or physical safety integration";
        } else if (level == device::CapabilityLevel::Execute &&
                   endpoint_.endpoint_is_physical) {
            truth.truth = device::CapabilityTruthValue::Unsupported;
            truth.why = "physical execute is not qualified; emulator-only E3 evidence cannot enable it";
        } else {
            truth.truth = device::CapabilityTruthValue::Verified;
            truth.evidence_ref = {"res:emulator_e3:built_in"};
        }
        report.levels.emplace(level, std::move(truth));
    }
    return report;
}

FreshValue<device::DeviceState> ManufacturingLane::deviceState() const {
    const auto snapshot = adapter_.snapshot();
    FreshValue<device::DeviceState> state;
    state.t_mono_ns = snapshot.t_mono_ns;
    state.seq = snapshot.seq;
    state.age_ms = snapshot.age_ms;
    state.ttl_ms = snapshot.ttl_ms;
    state.src = snapshot.src;
    state.state = snapshot.state;
    state.why = snapshot.why;
    if (snapshot.v) state.v = snapshot.v->state;
    if (snapshot.last_ok) state.last_ok = LastOk<device::DeviceState>{
        snapshot.last_ok->v.state, snapshot.last_ok->t_mono_ns, snapshot.last_ok->seq};
    if (!connected_) {
        state.v.reset();
        state.state = FreshState::Unknown;
        state.src = FreshSource::Declared;
        state.ttl_ms.reset();
        state.why = "emulator transport is not connected";
    }
    state.normalize();
    return state;
}

json ManufacturingLane::artifactJson(const ArtifactRecord& artifact) const {
    return {{"artifact_id", artifact.artifact_id}, {"device_id", artifact.device_id},
            {"kind", tier0::to_string(artifact.kind)}, {"hash_sha256", artifact.hash_sha256},
            {"bytes", artifact.bytes}, {"program_lines", artifact.program_lines.size()},
            {"post_profile_id", artifact.post_profile_id},
            {"post_profile_hash", artifact.post_profile_hash},
            {"slicer_profile_hash", artifact.slicer_profile_hash}, {"seq", artifact.seq}};
}

json ManufacturingLane::runJson(const RunRecord& run) const {
    json value{{"run_id", run.run_id}, {"device_id", run.device_id},
               {"artifact_id", run.artifact_id}, {"state", device::to_string(run.state)},
               {"approval_id", run.approval_id.empty() ? json(nullptr) : json(run.approval_id)},
               {"gate_report_ref", run.gate_report_ref.empty() ? json(nullptr) : json(run.gate_report_ref)},
               {"artifact_hash", artifacts_.at(run.artifact_id).hash_sha256},
               {"timeline_from_seq", run.created_at_seq}, {"seq", run.created_at_seq}};
    value["job"] = {{"job_id", run.job.job_id}, {"setup_revision", run.job.setup_revision},
                    {"setup_revision_seq", run.job.setup_revision_seq},
                    {"quantity", run.job.quantity}};
    if (run.initiator) value["initiator"] = initiatorJson(*run.initiator);
    else value["initiator"] = nullptr;
    return value;
}

json ManufacturingLane::proposalJson(const ApprovalProposal& proposal) const {
    return {{"proposal_id", proposal.proposal_id}, {"run_id", proposal.run_id},
            {"status", "unapproved"}, {"exercisable_by", to_string(proposal.exercisable_by)},
            {"expiry_ttl_ms", proposal.expiry_ttl_ms},
            {"attendance", {{"mode", to_string(proposal.attendance.mode)},
                            {"attested_by", proposal.attendance.attested_by},
                            {"at_seq", proposal.attendance.at_seq}}},
            {"spindle_authority", to_string(proposal.spindle_authority)}, {"seq", proposal.seq}};
}

json ManufacturingLane::approvalJson(const ApprovalBinding& approval) const {
    json value{{"approval_id", approval.approval_id}, {"run_id", approval.run_id},
               {"issued_at_seq", approval.issued_at_seq},
               {"machine", {{"device_id", approval.machine.device_id},
                             {"fingerprint_sha256", approval.machine.fingerprint_sha256}}},
               {"artifact", {{"hash_sha256", approval.artifact.hash_sha256},
                              {"bytes", approval.artifact.bytes},
                              {"kind", tier0::to_string(approval.artifact.kind)},
                              {"post_profile_id", approval.artifact.post_profile_id},
                              {"post_profile_hash", approval.artifact.post_profile_hash},
                              {"slicer_profile_hash", approval.artifact.slicer_profile_hash}}},
               {"job", {{"job_id", approval.job.job_id},
                         {"setup_revision", approval.job.setup_revision},
                         {"setup_revision_seq", approval.job.setup_revision_seq},
                         {"quantity", approval.job.quantity}}},
               {"preflight", {{"evidence", approval.preflight.evidence},
                               {"gate_report", approval.preflight.gate_report},
                               {"established_at_seq", approval.preflight.established_at_seq}}},
               {"approver", {{"identity", approval.approver.identity},
                              {"auth", approval.approver.auth},
                              {"at_mono_ns", approval.approver.at_mono_ns}}},
               {"expiry", {{"t_mono_ns", approval.expiry.t_mono_ns},
                            {"ttl_ms", approval.expiry.ttl_ms}}},
               {"exercisable_by", to_string(approval.exercisable_by)},
               {"nonce", {{"state", approval.nonce.state == NonceState::Unburned ? "unburned" :
                                             approval.nonce.state == NonceState::Burned ? "burned" : "void"},
                           {"burned_at_seq", approval.nonce.burned_at_seq
                               ? json(*approval.nonce.burned_at_seq) : json(nullptr)}}},
               {"max_starts", approval.max_starts},
               {"attendance", {{"mode", to_string(approval.attendance.mode)},
                                {"attested_by", approval.attendance.attested_by},
                                {"at_seq", approval.attendance.at_seq}}},
               {"spindle_authority", to_string(approval.spindle_authority)},
               {"policy_revision", approval.policy_revision}, {"seq", approval.issued_at_seq}};
    if (approval.initiator) value["initiator"] = initiatorJson(*approval.initiator);
    else value["initiator"] = nullptr;
    return value;
}

json ManufacturingLane::gateReportJson(const gates::GateReport& report) const {
    json stage1 = json::array();
    for (const auto& check : report.stage1) {
        stage1.push_back({{"name", check.name},
                          {"admission", gates::to_string(check.result.cls)},
                          {"admit", check.result.admit == gates::Admit::Admitted ? "admitted" : "rejected"},
                          {"why", check.result.why}});
    }
    json stage2 = json::array();
    for (const auto& guard : report.stage2)
        stage2.push_back({{"guard", gates::to_string(guard.guard)},
                          {"verdict", gates::to_string(guard.verdict)}, {"why", guard.why}});
    json required = json::array();
    for (const auto guard : report.required_set) required.push_back(gates::to_string(guard));
    json waivers = json::array();
    for (const auto& waiver : report.waivers)
        waivers.push_back({{"guard", gates::to_string(waiver.guard)}, {"by", waiver.by},
                           {"reason", waiver.reason}, {"at_seq", waiver.at_seq}});
    return {{"ref", report.ref}, {"seq", report.seq}, {"subject", report.subject},
            {"stage1", std::move(stage1)}, {"stage2", std::move(stage2)},
            {"required_set", std::move(required)}, {"waivers", std::move(waivers)},
            {"verdict", report.passed ? "pass" : "fail"}, {"rejecting", report.rejecting}};
}


gates::GateReport ManufacturingLane::computePreflight(RunRecord& run,
                                                        const ArtifactRecord& artifact) {
    gates::GateInput input;
    input.machine_class = device::MachineClass::Emulated;
    input.artifact_kind = artifact.kind;
    const auto snapshot = adapter_.snapshot();
    gates::Stage1Check controller;
    controller.name = "controller_snapshot";
    controller.subject = {device_id_.str(), "dev", epoch_.epoch, true, true,
                          device_id_.str(), device::MachineClass::Emulated};
    controller.candidate.offered_as = gates::Admission::ProbedEvidence;
    controller.candidate.fresh = gates::metadata(snapshot, "emulator", "manufacturing");
    controller.candidate.execute_truth = device::CapabilityTruthValue::Verified;
    controller.candidate.approval_device_id = device_id_.str();
    controller.accepts = {{"dev"}, {gates::Admission::ProbedEvidence}};
    input.stage1.push_back(std::move(controller));
    auto& facts = input.facts;
    facts.artifact_hash = artifact.hash_sha256;
    facts.approved_artifact_hash = artifact.hash_sha256;
    facts.machine_fingerprint = advertised_fingerprint_.str();
    facts.approved_machine_fingerprint = advertised_fingerprint_.str();
    facts.envelope_min = {0, 0, 0};
    facts.envelope_max = {0, 0, 0};
    facts.travel_min = {0, 0, 0};
    facts.travel_max = {300, 300, 80};
    facts.wcs_evidence_seq = std::max<device::Seq>(run.job.setup_revision_seq, 1);
    facts.setup_revision_seq = run.job.setup_revision_seq;
    facts.attendance_policy_satisfied = true;
    facts.spindle_authority_satisfied = true;
    facts.alarm_after_preflight = false;
    facts.nonce_unburned = true;
    facts.exercisable_by = ExercisableBy::HumanOnly;
    facts.initiator_kind = gates::InitiatorKind::Human;
    facts.now_mono_ns = clock_.mono_ns();
    facts.approval_expiry_mono_ns = facts.now_mono_ns + 300000000000ULL;
    facts.emulator_stage_passed = device::EmulatorStage::E3;
    facts.emulator_stage_required = device::EmulatorStage::E3;
    facts.interlock_closed = true;
    facts.feedhold_ack_hold0_observed = true;
    facts.metrology_tier = 2;
    facts.slicer_profile_hash = artifact.slicer_profile_hash;
    facts.approved_slicer_profile_hash = artifact.slicer_profile_hash;

    const std::string ref = device::ResultRef::mint("gate_report").str();
    const auto expected = journal_.head() + 1;
    auto report = gates::evaluate(input, run.run_id, ref, expected);
    const auto seq = appendEvent("mfg.run.gate_report", run.run_id, gateReportJson(report));
    report.seq = seq;
    run.gate_report_ref = ref;
    gate_reports_.emplace(ref, report);
    return report;
}

RunAuthorization ManufacturingLane::authorizationFor(const RunRecord& run,
                                                       const ArtifactRecord& artifact,
                                                       const ApprovalBinding& approval) const {
    RunAuthorization auth;
    auth.run_id = run.run_id;
    auth.approval_id = approval.approval_id;
    auth.state = device::RunState::Approved;
    auth.current_inputs = {approval.machine, approval.artifact, approval.job, approval.snapshot,
                           approval.preflight, approval.policy_revision, true};
    auth.gate_report_ref = run.gate_report_ref;
    auto& context = auth.tier0_context;
    context.action = tier0::Action::RunStart;
    context.artifact_kind = artifact.kind;
    context.machine_class = device::MachineClass::Emulated;
    context.policy = lanePolicy();
    context.policy.revision = approval.policy_revision;
    context.execute_capability = {device::CapabilityLevel::Execute,
        device::CapabilityTruthValue::Verified, {"res:emulator_e3:built_in"},
        device::EmulatorStage::E3, std::nullopt, {}};
    context.emulator_stage_passed = device::EmulatorStage::E3;
    context.commander_lease_held_by_caller = true;
    context.wcs_established_this_setup = true;
    context.bed_origin_declared_by_firmware = true;

    auto& input = auth.gate_input;
    input.machine_class = device::MachineClass::Emulated;
    input.artifact_kind = artifact.kind;
    const auto snapshot = adapter_.snapshot();
    gates::Stage1Check controller;
    controller.name = "controller_snapshot";
    controller.subject = {device_id_.str(), "dev", epoch_.epoch, true, true,
                          device_id_.str(), device::MachineClass::Emulated};
    controller.candidate.offered_as = gates::Admission::ProbedEvidence;
    controller.candidate.fresh = gates::metadata(snapshot, "emulator", "manufacturing");
    controller.candidate.execute_truth = device::CapabilityTruthValue::Verified;
    controller.candidate.approval_device_id = device_id_.str();
    controller.accepts = {{"dev"}, {gates::Admission::ProbedEvidence}};
    input.stage1.push_back(std::move(controller));
    auto& facts = input.facts;
    facts.artifact_hash = artifact.hash_sha256;
    facts.approved_artifact_hash = artifact.hash_sha256;
    facts.machine_fingerprint = advertised_fingerprint_.str();
    facts.approved_machine_fingerprint = advertised_fingerprint_.str();
    facts.envelope_min = {0, 0, 0};
    facts.envelope_max = {0, 0, 0};
    facts.travel_min = {0, 0, 0};
    facts.travel_max = {300, 300, 80};
    facts.wcs_evidence_seq = std::max<device::Seq>(approval.job.setup_revision_seq, 1);
    facts.setup_revision_seq = approval.job.setup_revision_seq;
    facts.attendance_policy_satisfied = true;
    facts.spindle_authority_satisfied = true;
    facts.alarm_after_preflight = false;
    facts.nonce_unburned = true;
    facts.exercisable_by = approval.exercisable_by;
    facts.now_mono_ns = clock_.mono_ns();
    facts.approval_expiry_mono_ns = approval.expiry.t_mono_ns;
    facts.emulator_stage_passed = device::EmulatorStage::E3;
    facts.emulator_stage_required = device::EmulatorStage::E3;
    facts.interlock_closed = true;
    facts.feedhold_ack_hold0_observed = true;
    facts.metrology_tier = 2;
    facts.slicer_profile_hash = artifact.slicer_profile_hash;
    facts.approved_slicer_profile_hash = artifact.slicer_profile_hash;
    return auth;
}

tier0::ArtifactKind ManufacturingLane::parseArtifactKind(std::string_view value) {
    if (value == "nc") return tier0::ArtifactKind::Nc;
    if (value == "gcode") return tier0::ArtifactKind::Gcode;
    if (value == "print_job") return tier0::ArtifactKind::PrintJob;
    throw std::invalid_argument("artifact.kind must be nc, gcode, or print_job");
}

ExercisableBy ManufacturingLane::parseExercisableBy(std::string_view value) {
    if (value == "human_only") return ExercisableBy::HumanOnly;
    if (value == "human_or_agent") return ExercisableBy::HumanOrAgent;
    throw std::invalid_argument("exercisable_by must be human_only or human_or_agent");
}

AttendanceMode ManufacturingLane::parseAttendanceMode(std::string_view value) {
    if (value == "physical_attended") return AttendanceMode::PhysicalAttended;
    if (value == "remote_attended") return AttendanceMode::RemoteAttended;
    if (value == "unattended") return AttendanceMode::Unattended;
    throw std::invalid_argument("invalid attendance mode");
}

SpindleAuthority ManufacturingLane::parseSpindleAuthority(std::string_view value) {
    if (value == "not_applicable") return SpindleAuthority::NotApplicable;
    if (value == "manual_confirmed") return SpindleAuthority::ManualConfirmed;
    if (value == "commanded_feedback") return SpindleAuthority::CommandedFeedback;
    throw std::invalid_argument("invalid spindle_authority");
}

Initiator ManufacturingLane::parseInitiator(const json& value) {
    if (!value.is_object()) throw std::invalid_argument("initiator object is required");
    Initiator out;
    const std::string kind = value.value("kind", std::string{});
    if (kind == "human") out.kind = gates::InitiatorKind::Human;
    else if (kind == "agent") out.kind = gates::InitiatorKind::Agent;
    else throw std::invalid_argument("initiator.kind must be human or agent");
    out.identity = value.value("identity", std::string{});
    out.seat = value.value("seat", std::string{});
    if (out.identity.empty() || out.seat.empty())
        throw std::invalid_argument("initiator identity and seat are required");
    if (value.contains("agent_run_id") && !value.at("agent_run_id").is_null())
        out.agent_run_id = value.at("agent_run_id").get<std::string>();
    if (out.kind == gates::InitiatorKind::Agent && !out.agent_run_id)
        throw std::invalid_argument("agent initiator requires agent_run_id");
    return out;
}

LaneRpcResult ManufacturingLane::route(std::string_view method, const json& params) {
    LaneRpcResult failure;

    if (method == "mfg_device_list") {
        serviceDevice();
        const auto lease = leases_.describe(device_id_.str(), clock_.mono_ns());
        json row{{"device_id", device_id_.str()}, {"machine_class", "emulated"},
                 {"state", deviceState()},
                 {"capability_summary",
                  {{"max_advertised", endpoint_.endpoint_is_physical ? "transport" : "execute"},
                   {"emulator_stage_passed", "E3"}}},
                 {"lease", lease ? leaseJson(*lease) : json::object()}};
        return ok({{"devices", json::array({std::move(row)})}});
    }
    if (method == "mfg_device_describe") {
        if (!validDevice(params, failure)) return failure;
        serviceDevice();
        return ok({{"identity", advertised_identity_.canonical()},
                   {"fingerprint_sha256", advertised_fingerprint_.str()},
                   {"capability", capabilityReport()},
                   {"transport", {{"kind", "emulator"}, {"endpoint_id", endpoint_.endpoint_id},
                                  {"endpoint_is_physical", endpoint_.endpoint_is_physical},
                                  {"connected", connected_}}},
                   {"policy", policyJson(lanePolicy())}});
    }
    if (method == "mfg_device_capability") {
        if (!validDevice(params, failure)) return failure;
        return ok({{"capability", capabilityReport()}});
    }
    if (method == "mfg_device_claim") {
        if (!validDevice(params, failure)) return failure;
        const std::string role = params.value("role", "commander");
        if (role != "commander")
            return refuse("lease.observer_has_no_command_lease",
                          "observer clients read without owning the command lease");
        const json owner_json = params.value("owner", json::object());
        LeaseOwner owner{owner_json.value("client_kind", "unknown"),
                         owner_json.value("seat", "local"),
                         owner_json.value("session", device::detail::mint_ulid())};
        const uint64_t ttl = params.value("ttl_ms", 15000ULL);
        const auto claimed = leases_.claim(device_id_.str(), owner, clock_.mono_ns(), ttl,
                                           std::min<uint64_t>(3000, ttl), "emulator",
                                           endpoint_.endpoint_id);
        if (!claimed.ok)
            return refuse(claimed.code, claimed.why,
                          {{"current_owner", claimed.current_owner
                              ? json{{"client_kind", claimed.current_owner->client_kind},
                                     {"seat", claimed.current_owner->seat},
                                     {"session", claimed.current_owner->session}}
                              : json(nullptr)}});
        const auto seq = journal_.head();
        publishEvent("mfg.device.lease", device_id_.str(), seq,
                     {{"device_id", device_id_.str()}, {"lease", leaseJson(*claimed.lease)},
                      {"change", "granted"}});
        return ok({{"lease", leaseJson(*claimed.lease)}});
    }
    if (method == "mfg_device_release") {
        if (!requireLease(params, failure)) return failure;
        const auto active = leases_.describe(device_id_.str(), clock_.mono_ns());
        const auto released = leases_.release(active->lease_id, active->owner, clock_.mono_ns());
        if (!released.ok) return refuse(released.code, released.why);
        const auto seq = journal_.head();
        publishEvent("mfg.device.lease", device_id_.str(), seq,
                     {{"device_id", device_id_.str()}, {"lease", leaseJson(*active)},
                      {"change", "released"}});
        return ok({{"released", true}});
    }
    if (method == "mfg_device_connect") {
        if (!requireLease(params, failure)) return failure;
        if (connected_) return ok({{"connected", true}, {"already_connected", true}});
        const auto opened = adapter_.open(endpoint_);
        if (!opened.ok) return refuse("mfg_transport_open_refused", opened.why);
        connected_ = true;
        const auto state_seq = appendEvent("mfg.device.state", device_id_.str(),
            {{"device_id", device_id_.str()}, {"state", deviceState()}, {"raw", "connecting"}});
        for (int i = 0; i < 64 && !adapter_.fingerprint(); ++i)
            adapter_.service(state_seq);
        if (!adapter_.fingerprint()) {
            connected_ = false;
            adapter_.close();
            return refuse("mfg_device_identification_failed", "emulator identity did not complete");
        }
        clock_.advance_ns(200'000'000);
        bool idle = false;
        for (int i = 0; i < 64; ++i) {
            adapter_.service(state_seq);
            const auto current = adapter_.snapshot();
            if (current.v && current.v->state == device::DeviceState::Idle) {
                idle = true;
                break;
            }
        }
        if (!idle) {
            connected_ = false;
            adapter_.close();
            return refuse("mfg_device_identification_failed",
                          "emulator did not publish an idle snapshot");
        }
        const auto snapshot = adapter_.snapshot();
        appendEvent("mfg.device.snapshot", device_id_.str(),
                    {{"device_id", device_id_.str()}, {"snapshot", snapshot}});
        return ok({{"connected", true}, {"device_id", device_id_.str()},
                   {"fingerprint_sha256", advertised_fingerprint_.str()}});
    }
    if (method == "mfg_device_disconnect") {
        if (!requireLease(params, failure)) return failure;
        if (connected_) adapter_.close();
        connected_ = false;
        for (auto& [id, run] : runs_) {
            if (run.state == device::RunState::Held) arbiter_.record_discontinuity(id,
                    device::RunDiscontinuity::Disconnect);
        }
        appendEvent("mfg.device.state", device_id_.str(),
                    {{"device_id", device_id_.str()}, {"state", deviceState()},
                     {"raw", "transport closed"}});
        return ok({{"disconnected", true}});
    }
    if (method == "mfg_telemetry_snapshot") {
        if (!validDevice(params, failure)) return failure;
        serviceDevice();
        auto snapshot = adapter_.snapshot();
        if (!connected_) {
            snapshot = {};
            snapshot.state = FreshState::Unknown;
            snapshot.src = FreshSource::Declared;
            snapshot.why = "emulator transport is not connected";
            snapshot.ttl_ms.reset();
            snapshot.normalize();
        }
        return ok({{"snapshot", snapshot},
                   {"raw", snapshot.v ? json(snapshot.v->raw) : json(nullptr)}});
    }
    if (method == "mfg_artifact_register") {
        if (!requireLease(params, failure)) return failure;
        if (!params.contains("artifact") || !params.at("artifact").is_object())
            return refuse("mfg_artifact_invalid", "artifact object is required");
        const auto& input = params.at("artifact");
        ArtifactRecord artifact;
        artifact.artifact_id = device::ResultRef::mint("artifact").str();
        artifact.device_id = device_id_.str();
        artifact.kind = parseArtifactKind(input.value("kind", std::string{}));
        std::string content;
        if (input.contains("lines")) {
            artifact.program_lines = input.at("lines").get<std::vector<std::string>>();
            for (const auto& line : artifact.program_lines) content += line + "\n";
        } else if (input.contains("content")) {
            content = input.at("content").get<std::string>();
            artifact.program_lines = linesFromText(content);
        } else if (input.contains("path")) {
            std::ifstream stream(input.at("path").get<std::string>(), std::ios::binary);
            if (!stream) return refuse("mfg_artifact_path_unreadable", "artifact path cannot be read");
            content.assign(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
            artifact.program_lines = linesFromText(content);
        } else {
            return refuse("mfg_artifact_content_missing", "artifact requires content, lines, or path");
        }
        if (content.empty()) return refuse("mfg_artifact_empty", "artifact content is empty");
        artifact.bytes = content.size();
        artifact.hash_sha256 = device::detail::sha256_hex(content);
        if (input.contains("hash_sha256") && input.at("hash_sha256") != artifact.hash_sha256)
            return refuse("mfg_artifact_hash_mismatch", "caller hash does not match artifact bytes");
        artifact.post_profile_id = input.value("post_profile_id", "emulator-default");
        artifact.post_profile_hash = input.value("post_profile_hash",
                                                 device::detail::sha256_hex("emulator-default"));
        artifact.slicer_profile_hash = input.value("slicer_profile_hash", std::string{});
        const auto expected = journal_.head() + 1;
        artifact.seq = expected;
        const auto seq = appendEvent("mfg.artifact.registered", artifact.artifact_id,
                                     artifactJson(artifact));
        artifact.seq = seq;
        artifacts_.emplace(artifact.artifact_id, artifact);
        return ok({{"artifact", artifactJson(artifact)}});
    }
    if (method == "mfg_preflight_run") {
        if (!requireLease(params, failure)) return failure;
        if (!connected_) return refuse("mfg_transport_not_connected",
                                       "preflight requires a connected emulator snapshot");
        serviceDevice();
        const std::string artifact_id = params.value("artifact_id", std::string{});
        const auto artifact_it = artifacts_.find(artifact_id);
        if (artifact_it == artifacts_.end())
            return refuse("mfg_artifact_not_found", "artifact_id is not registered");
        RunRecord* run = nullptr;
        const std::string requested_run = params.value("run_id", std::string{});
        if (!requested_run.empty()) {
            const auto existing = runs_.find(requested_run);
            if (existing == runs_.end()) return refuse("mfg_run_not_found", "run_id is not registered");
            if (existing->second.artifact_id != artifact_id)
                return refuse("mfg_run_artifact_mismatch", "run is bound to a different artifact");
            run = &existing->second;
        } else {
            RunRecord created;
            created.run_id = device::RunId::mint().str();
            created.device_id = device_id_.str();
            created.artifact_id = artifact_id;
            const auto job = params.value("job", json::object());
            created.job.job_id = job.value("job_id", "job:" + device::detail::mint_ulid());
            created.job.setup_revision = job.value("setup_revision", 1ULL);
            created.job.setup_revision_seq = journal_.head();
            created.job.quantity = job.value("quantity", 1U);
            const auto snap = params.value("snapshot", json::object());
            created.snapshot.tool_table_hash = snap.value("tool_table_hash", "emulator-tools");
            created.snapshot.fixture_hash = snap.value("fixture_hash", "emulator-fixture");
            const auto wcs = snap.value("wcs", json::object());
            created.snapshot.wcs.system = wcs.value("system", "G54");
            created.snapshot.wcs.offset = wcs.value("offset", std::vector<double>{0, 0, 0});
            created.snapshot.wcs.established_by = wcs.value("established_by", "emulator-origin");
            created.created_at_seq = appendEvent("mfg.run.state", created.run_id,
                {{"run_id", created.run_id}, {"state", "draft"}, {"cause", "preflight_created"}});
            auto [it, inserted] = runs_.emplace(created.run_id, std::move(created));
            run = &it->second;
        }
        tier0::Context tier;
        tier.action = tier0::Action::RunSubmit;
        tier.artifact_kind = artifact_it->second.kind;
        tier.machine_class = device::MachineClass::Emulated;
        tier.artifact_present = true;
        tier.artifact_hashed = !artifact_it->second.hash_sha256.empty();
        tier.post_profile_bound = artifact_it->second.kind == tier0::ArtifactKind::PrintJob ||
                                  !artifact_it->second.post_profile_hash.empty();
        tier.policy = lanePolicy();
        const auto refusals = tier0::evaluate(tier);
        json tier_json = json::array();
        for (const auto& refusal : refusals)
            tier_json.push_back({{"action", refusal.action}, {"prerequisite", refusal.prerequisite},
                                 {"why", refusal.why}, {"missing_level", refusal.missing_level},
                                 {"remedy", refusal.remedy},
                                 {"artifact_dependent", refusal.artifact_dependent}});
        const auto report = computePreflight(*run, artifact_it->second);
        return ok({{"run_id", run->run_id}, {"state", device::to_string(run->state)},
                   {"tier0", std::move(tier_json)}, {"gate_report", gateReportJson(report)}});
    }
    if (method == "mfg_gate_report") {
        const std::string ref = params.value("gate_report_ref", std::string{});
        const auto it = gate_reports_.find(ref);
        if (it == gate_reports_.end())
            return refuse("mfg_gate_report_not_found", "gate_report_ref is not registered");
        return ok({{"gate_report", gateReportJson(it->second)}});
    }
    if (method == "mfg_approval_propose") {
        if (!requireLease(params, failure)) return failure;
        const std::string run_id = params.value("run_id", std::string{});
        const auto run = runs_.find(run_id);
        if (run == runs_.end()) return refuse("mfg_run_not_found", "run_id is not registered");
        if (run->second.gate_report_ref.empty())
            return refuse("mfg_preflight_missing", "approval proposal requires a preflight report");
        ApprovalProposal proposal;
        proposal.proposal_id = "proposal:" + device::detail::mint_ulid();
        proposal.run_id = run_id;
        proposal.exercisable_by = parseExercisableBy(params.value("exercisable_by", "human_only"));
        proposal.expiry_ttl_ms = params.value("expiry_ttl_ms", 300000ULL);
        const auto attendance = params.value("attendance", json::object());
        proposal.attendance.mode = parseAttendanceMode(attendance.value("mode", "physical_attended"));
        proposal.attendance.attested_by = attendance.value("attested_by", std::string{});
        proposal.spindle_authority = parseSpindleAuthority(
            params.value("spindle_authority", "not_applicable"));
        const auto policy = lanePolicy();
        if (!permits(policy, proposal.exercisable_by)) {
            return refuse("tier0:exercisable_by_exceeds_policy",
                          "requested exercisable_by exceeds commissioning policy");
        }
        proposal.seq = journal_.head() + 1;
        proposal.attendance.at_seq = proposal.seq;
        proposal.seq = appendEvent("mfg.approval.proposed", proposal.proposal_id,
                                   proposalJson(proposal));
        proposals_.emplace(proposal.proposal_id, proposal);
        return ok({{"proposal", proposalJson(proposal)}});
    }
    if (method == "mfg_approval_create") {
        if (!requireLease(params, failure)) return failure;
        const std::string proposal_id = params.value("proposal_id", std::string{});
        const auto proposal = proposals_.find(proposal_id);
        if (proposal == proposals_.end())
            return refuse("mfg_approval_proposal_not_found", "proposal_id is not registered");
        const auto auth = params.value("approver_auth", json::object());
        const std::string identity = auth.value("identity", std::string{});
        const std::string method_auth = auth.value("auth", std::string{});
        if (identity.empty() || method_auth.empty())
            return refuse("mfg_approval_human_auth_required",
                          "approval creation requires authenticated human identity and auth method");
        const auto run_it = runs_.find(proposal->second.run_id);
        const auto artifact_it = artifacts_.find(run_it->second.artifact_id);
        const auto report_it = gate_reports_.find(run_it->second.gate_report_ref);
        ApprovalBinding approval;
        approval.approval_id = device::ApprovalId::mint().str();
        approval.run_id = run_it->second.run_id;
        approval.issued_at_seq = journal_.head() + 1;
        approval.machine = {device_id_.str(), advertised_fingerprint_.str()};
        approval.artifact = {artifact_it->second.hash_sha256, artifact_it->second.bytes,
                             artifact_it->second.kind, artifact_it->second.post_profile_id,
                             artifact_it->second.post_profile_hash,
                             artifact_it->second.slicer_profile_hash};
        approval.job = run_it->second.job;
        approval.snapshot = run_it->second.snapshot;
        approval.preflight = {{run_it->second.gate_report_ref}, run_it->second.gate_report_ref,
                              report_it->second.seq};
        approval.approver = {identity, method_auth, clock_.mono_ns()};
        approval.expiry = {approval.approver.at_mono_ns +
                               proposal->second.expiry_ttl_ms * 1000000ULL,
                           proposal->second.expiry_ttl_ms};
        approval.exercisable_by = proposal->second.exercisable_by;
        approval.nonce = {device::detail::mint_ulid(), NonceState::Unburned, std::nullopt};
        approval.max_starts = 1;
        approval.attendance = proposal->second.attendance;
        approval.spindle_authority = proposal->second.spindle_authority;
        approval.policy_revision = lanePolicy().revision;
        std::string why;
        const auto before = journal_.head();
        if (!arbiter_.register_approval(approval, &why))
            return refuse("mfg_approval_create_refused", why);
        approval.issued_at_seq = journal_.head();
        approvals_.emplace(approval.approval_id, approval);
        run_it->second.approval_id = approval.approval_id;
        run_it->second.state = device::RunState::Approved;
        if (!arbiter_.register_run(authorizationFor(run_it->second, artifact_it->second, approval), &why))
            return refuse("mfg_run_authorization_registration_refused", why);
        for (const auto& record : journal_.replay(before + 1))
            publishEvent(record.kind, record.subject, record.seq, record.body);
        publishEvent("mfg.run.state", run_it->second.run_id, journal_.head(),
                     {{"run_id", run_it->second.run_id}, {"state", "approved"},
                      {"cause", "human_approval_created"}});
        return ok({{"approval", approvalJson(approval)}});
    }
    if (method == "mfg_approval_inspect") {
        const std::string approval_id = params.value("approval_id", std::string{});
        if (!approval_id.empty()) {
            const auto it = approvals_.find(approval_id);
            if (it == approvals_.end()) return refuse("mfg_approval_not_found", "approval_id is not registered");
            auto value = approvalJson(it->second);
            value["status"] = it->second.nonce.state == NonceState::Burned ? "burned" :
                              it->second.nonce.state == NonceState::Void ? "revoked" : "active";
            return ok({{"approval", std::move(value)}});
        }
        const std::string proposal_id = params.value("proposal_id", std::string{});
        const auto proposal = proposals_.find(proposal_id);
        if (proposal == proposals_.end())
            return refuse("mfg_approval_not_found", "approval_id or proposal_id is required");
        return ok({{"approval", proposalJson(proposal->second)}});
    }
    if (method == "mfg_approval_revoke") {
        if (!requireLease(params, failure)) return failure;
        const std::string approval_id = params.value("approval_id", std::string{});
        const auto approval = approvals_.find(approval_id);
        if (approval == approvals_.end()) return refuse("mfg_approval_not_found", "approval_id is not registered");
        const std::string reason = params.value("reason", std::string{});
        if (reason.empty()) return refuse("mfg_revoke_reason_required", "revocation requires a reason");
        InvalidationNotice notice;
        notice.classification = InvalidationClass::Falsified;
        notice.reason = "approval_revoked";
        notice.why = reason;
        notice.affected = {approval_id, approval->second.run_id};
        notice.at_seq = journal_.head() + 1;
        const auto seq = appendEvent("mfg.result.falsified", approval_id, notice);
        notice.at_seq = seq;
        invalidations_.push_back(notice);
        appendEvent("mfg.approval.revoked", approval_id,
                    {{"approval_id", approval_id}, {"run_id", approval->second.run_id},
                     {"at_seq", seq}, {"reason", reason}});
        approval->second.nonce.state = NonceState::Void;
        return ok({{"revoked", true}, {"at_seq", seq}});
    }
    if (method == "mfg_run_start") {
        if (!requireLease(params, failure)) return failure;
        if (endpoint_.endpoint_is_physical)
            return refuse("mfg_physical_execute_not_qualified",
                          "physical execute is not advertised or enabled in P4");
        if (!connected_) return refuse("mfg_transport_not_connected", "run start requires a connected device");
        const std::string run_id = params.value("run_id", std::string{});
        const std::string approval_id = params.value("approval_id", std::string{});
        const auto run = runs_.find(run_id);
        if (run == runs_.end()) return refuse("mfg_run_not_found", "run_id is not registered");
        const auto approval = approvals_.find(approval_id);
        if (approval == approvals_.end() || approval->second.nonce.state == NonceState::Void)
            return refuse("approval.revoked_or_missing", "approval is absent or revoked");
        serviceDevice();
        const auto initiator = parseInitiator(params.at("initiator"));
        const auto before = journal_.head();
        const auto outcome = arbiter_.start(run_id, approval_id, initiator);
        for (const auto& record : journal_.replay(before + 1))
            publishEvent(record.kind, record.subject, record.seq, record.body);
        if (!outcome.ok) {
            json detail{{"state", device::to_string(outcome.state)}};
            if (outcome.gate_report) detail["gate_report"] = gateReportJson(*outcome.gate_report);
            return refuse(outcome.code, outcome.why, std::move(detail));
        }
        auto started = adapter_.startProgram(artifacts_.at(run->second.artifact_id).program_lines);
        if (!started.ok)
            return refuse("mfg_program_start_refused_after_nonce_burn", started.why,
                          {{"nonce_burned_at_seq", *outcome.nonce_burned_at_seq},
                           {"snapshot", adapter_.snapshot()}});
        run->second.state = outcome.state;
        const auto described = arbiter_.describe_run(run_id);
        if (described && described->initiator) run->second.initiator = described->initiator;
        approval->second.nonce.state = NonceState::Burned;
        approval->second.nonce.burned_at_seq = outcome.nonce_burned_at_seq;
        approval->second.initiator = run->second.initiator;
        return ok({{"state", device::to_string(outcome.state)},
                   {"nonce_burned_at_seq", *outcome.nonce_burned_at_seq},
                   {"gate_report_ref", outcome.gate_report ? outcome.gate_report->ref : run->second.gate_report_ref}});
    }
    if (method == "mfg_run_hold") {
        if (!requireLease(params, failure)) return failure;
        const auto initiator = parseInitiator(params.at("initiator"));
        const std::string run_id = params.value("run_id", std::string{});
        HoldKind kind = HoldKind::FeedHold;
        const std::string kind_name = params.value("kind", "feed_hold");
        if (kind_name == "controlled_stop") kind = HoldKind::ControlledStop;
        else if (kind_name == "door_suspend") kind = HoldKind::DoorSuspend;
        else if (kind_name != "feed_hold") return refuse("mfg_hold_kind_invalid", "invalid hold kind");
        const auto before = journal_.head();
        const auto outcome = arbiter_.hold(run_id, initiator, kind);
        for (const auto& record : journal_.replay(before + 1)) publishEvent(record.kind, record.subject, record.seq, record.body);
        if (!outcome.ok) return refuse(outcome.code, outcome.why);
        const auto device_outcome = adapter_.requestHold();
        if (!device_outcome.ok) return refuse("mfg_device_hold_refused", device_outcome.why);
        runs_.at(run_id).state = outcome.state;
        return ok({{"state", device::to_string(outcome.state)}, {"at_seq", *outcome.at_seq}});
    }
    if (method == "mfg_run_resume") {
        if (!requireLease(params, failure)) return failure;
        const auto initiator = parseInitiator(params.at("initiator"));
        const std::string run_id = params.value("run_id", std::string{});
        const auto run = arbiter_.describe_run(run_id);
        if (!run) return refuse("run.not_found", "run is not registered");
        if (run->state != device::RunState::Held)
            return refuse("run.not_held", "resume refusal applies only to a held run");
        std::string discontinuity = "none";
        std::string code = "resume_requires_new_authored_run";
        switch (run->discontinuity_since_hold) {
            case device::RunDiscontinuity::None: break;
            case device::RunDiscontinuity::Reset:
                discontinuity = "reset"; code = "resume_forbidden_after_reset"; break;
            case device::RunDiscontinuity::Alarm:
                discontinuity = "alarm"; code = "no_alarm_since_hold"; break;
            case device::RunDiscontinuity::HardLimit:
                discontinuity = "hard_limit"; code = "resume_forbidden_after_hard_limit"; break;
            case device::RunDiscontinuity::Disconnect:
                discontinuity = "disconnect"; code = "resume_forbidden_after_disconnect"; break;
            case device::RunDiscontinuity::PowerLoss:
                discontinuity = "power_loss"; code = "resume_forbidden_after_power_loss"; break;
        }
        const auto seq = appendEvent("mfg.arbiter.attempt", run_id,
            {{"action", "resume"},
             {"initiator", {{"kind", initiator.kind == gates::InitiatorKind::Human ? "human" : "agent"},
                            {"identity", initiator.identity}, {"seat", initiator.seat}}},
             {"outcome", "refused"}, {"reason", code}, {"discontinuity", discontinuity}});
        return refuse(code,
            "resume is disabled after hold (discontinuity=" + discontinuity +
                "); author a new run with fresh inspection, preflight, and approval",
            {{"at_seq", seq}, {"discontinuity", discontinuity},
             {"required", "new authored run with fresh approval"}});
    }
    if (method == "mfg_run_abort") {
        if (!requireLease(params, failure)) return failure;
        const auto initiator = parseInitiator(params.at("initiator"));
        const std::string run_id = params.value("run_id", std::string{});
        const auto before = journal_.head();
        const auto outcome = arbiter_.abort(run_id, initiator);
        for (const auto& record : journal_.replay(before + 1)) publishEvent(record.kind, record.subject, record.seq, record.body);
        if (!outcome.ok) return refuse(outcome.code, outcome.why);
        const auto device_outcome = adapter_.abort();
        if (!device_outcome.ok) return refuse("mfg_device_abort_refused", device_outcome.why);
        runs_.at(run_id).state = outcome.state;
        arbiter_.record_discontinuity(run_id, device::RunDiscontinuity::Reset);
        return ok({{"state", device::to_string(outcome.state)}, {"at_seq", *outcome.at_seq}});
    }
    if (method == "mfg_run_list") {
        json rows = json::array();
        const std::string device_filter = params.value("device_id", std::string{});
        const std::string state_filter = params.value("state", std::string{});
        const size_t limit = params.value("limit", size_t{100});
        for (auto it = runs_.rbegin(); it != runs_.rend() && rows.size() < limit; ++it) {
            if (!device_filter.empty() && it->second.device_id != device_filter) continue;
            if (!state_filter.empty() && device::to_string(it->second.state) != state_filter) continue;
            rows.push_back(runJson(it->second));
        }
        return ok({{"runs", std::move(rows)}});
    }
    if (method == "mfg_run_describe") {
        const std::string run_id = params.value("run_id", std::string{});
        const auto run = runs_.find(run_id);
        if (run == runs_.end()) return refuse("mfg_run_not_found", "run_id is not registered");
        return ok({{"run", runJson(run->second)}});
    }
    if (method == "mfg_journal_query") {
        const auto from = params.value("from_seq", device::Seq{1});
        const auto subject = params.value("subject", std::string{});
        const auto kinds = params.value("kinds", std::vector<std::string>{});
        const size_t limit = params.value("limit", size_t{1000});
        json records = json::array();
        for (const auto& record : journal_.replay(from)) {
            if (!subject.empty() && record.subject != subject) continue;
            if (!kinds.empty() && std::find(kinds.begin(), kinds.end(), record.kind) == kinds.end()) continue;
            records.push_back(record);
            if (records.size() >= limit) break;
        }
        return ok({{"records", std::move(records)}, {"head_seq", journal_.head()},
                   {"waiver_rates", json::object()}});
    }
    if (method == "mfg_result_invalidations") {
        const auto from = params.value("from_seq", device::Seq{1});
        const std::string affected = params.value("affected", std::string{});
        const size_t limit = params.value("limit", size_t{1000});
        json notices = json::array();
        for (const auto& record : journal_.replay(from)) {
            if (record.kind != "mfg.result.falsified" && record.kind != "mfg.result.superseded") continue;
            json notice = record.body;
            notice["at_seq"] = record.seq;
            if (!affected.empty()) {
                const auto rows = notice.value("affected", std::vector<std::string>{});
                if (std::find(rows.begin(), rows.end(), affected) == rows.end()) continue;
            }
            notices.push_back(std::move(notice));
            if (notices.size() >= limit) break;
        }
        return ok({{"invalidations", std::move(notices)}, {"head_seq", journal_.head()}});
    }
    if (method == "mfg_emulator_fault_inject") {
        if (!requireLease(params, failure)) return failure;
        if (endpoint_.endpoint_is_physical)
            return refuse("mfg_emulator_fault_physical_endpoint_forbidden",
                          "emulator fault injection is forbidden for a physical endpoint");
        if (!connected_)
            return refuse("mfg_transport_not_connected",
                          "fault injection requires a connected emulator transport");
        const std::string fault = params.value("fault", std::string{});
        device::EmulatorFault value;
        device::RunDiscontinuity discontinuity = device::RunDiscontinuity::None;
        if (fault == "alarm") { value = device::EmulatorFault::Alarm; discontinuity = device::RunDiscontinuity::Alarm; }
        else if (fault == "hard_limit") { value = device::EmulatorFault::HardLimit; discontinuity = device::RunDiscontinuity::HardLimit; }
        else if (fault == "disconnect") { value = device::EmulatorFault::Disconnect; discontinuity = device::RunDiscontinuity::Disconnect; }
        else if (fault == "door") value = device::EmulatorFault::Door;
        else return refuse("mfg_emulator_fault_invalid", "fault must be alarm, hard_limit, disconnect, or door");
        emulator_.injectFault(value);
        adapter_.service(journal_.head());
        if (discontinuity != device::RunDiscontinuity::None) {
            for (auto& [id, run] : runs_)
                if (run.state == device::RunState::Held)
                    arbiter_.record_discontinuity(id, discontinuity);
        }
        const auto snapshot = adapter_.snapshot();
        appendEvent(fault == "alarm" || fault == "hard_limit" ? "mfg.device.alarm" : "mfg.device.state",
                    device_id_.str(), {{"device_id", device_id_.str()}, {"snapshot", snapshot},
                                       {"fault", fault}});
        return ok({{"injected", true}, {"fault", fault}, {"snapshot", snapshot}});
    }

    return refuse("mfg_unknown_method", "unknown manufacturing RPC method",
                  {{"method", method}});
}

ManufacturingLane& lane() {
    static ManufacturingLane instance(defaultStateDirectory());
    return instance;
}

}  // namespace chudware::mfg
