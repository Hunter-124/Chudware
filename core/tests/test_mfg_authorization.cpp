#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <filesystem>
#include <string>

#include "manufacturing/approval.h"
#include "manufacturing/arbiter.h"
#include "manufacturing/gates.h"
#include "manufacturing/lease.h"
#include "manufacturing/policy.h"
#include "manufacturing/tier0.h"

namespace {

using namespace chudware;

struct TempDirectory {
    std::filesystem::path path = std::filesystem::temp_directory_path() /
        ("chudware-mfg-auth-" + device::detail::mint_ulid());
    TempDirectory() { std::filesystem::create_directories(path); }
    ~TempDirectory() {
        std::error_code ignored;
        std::filesystem::remove_all(path, ignored);
    }
};

struct FakeClock final : mfg::Clock {
    uint64_t now = 1000000;
    uint64_t mono_ns() const override { return now; }
};

mfg::gates::GuardFacts passing_facts() {
    mfg::gates::GuardFacts facts;
    facts.artifact_hash = "artifact";
    facts.approved_artifact_hash = "artifact";
    facts.machine_fingerprint = "fingerprint";
    facts.approved_machine_fingerprint = "fingerprint";
    facts.envelope_min = {0, 0, 0};
    facts.envelope_max = {9, 9, 9};
    facts.travel_min = {0, 0, 0};
    facts.travel_max = {10, 10, 10};
    facts.wcs_evidence_seq = 20;
    facts.setup_revision_seq = 10;
    facts.attendance_policy_satisfied = true;
    facts.spindle_authority_satisfied = true;
    facts.alarm_after_preflight = false;
    facts.nonce_unburned = true;
    facts.exercisable_by = mfg::ExercisableBy::HumanOrAgent;
    facts.initiator_kind = mfg::gates::InitiatorKind::Agent;
    facts.now_mono_ns = 100;
    facts.approval_expiry_mono_ns = 101;
    facts.emulator_stage_passed = device::EmulatorStage::E6;
    facts.emulator_stage_required = device::EmulatorStage::E6;
    facts.interlock_closed = true;
    facts.feedhold_ack_hold0_observed = true;
    facts.metrology_tier = 2;
    facts.slicer_profile_hash = "slicer";
    facts.approved_slicer_profile_hash = "slicer";
    return facts;
}

void make_guard_fail(mfg::gates::Guard guard, mfg::gates::GuardFacts& facts) {
    using G = mfg::gates::Guard;
    switch (guard) {
        case G::ArtifactHashMatchesApproval: facts.artifact_hash = "reposted"; break;
        case G::MachineFingerprintMatches: facts.machine_fingerprint = "settings-edited"; break;
        case G::EnvelopeWithinTravel: facts.envelope_max[0] = 13; break;
        case G::WcsEstablishedThisSetup: facts.wcs_evidence_seq = 9; break;
        case G::AttendancePolicySatisfied: facts.attendance_policy_satisfied = false; break;
        case G::SpindleAuthoritySatisfied: facts.spindle_authority_satisfied = false; break;
        case G::AlarmHistoryClearSincePreflight: facts.alarm_after_preflight = true; break;
        case G::NonceUnburned: facts.nonce_unburned = false; break;
        case G::ExercisableByPermitsInitiator:
            facts.exercisable_by = mfg::ExercisableBy::HumanOnly;
            facts.initiator_kind = mfg::gates::InitiatorKind::Agent;
            break;
        case G::ApprovalUnexpiredAtSeq: facts.now_mono_ns = facts.approval_expiry_mono_ns; break;
        case G::EmulatorStageAtLeast: facts.emulator_stage_passed = device::EmulatorStage::E5; break;
        case G::InterlockEngaged: facts.interlock_closed = false; break;
        case G::FeedholdAckObserved: facts.feedhold_ack_hold0_observed = false; break;
        case G::TierAtLeastQ2: facts.metrology_tier = 1; break;
        case G::SlicerProfileHashMatches: facts.slicer_profile_hash = "edited"; break;
    }
}

mfg::ApprovalBinding make_approval(std::string run_id, std::string approval_id,
                                   mfg::ExercisableBy exercisable,
                                   uint64_t expiry = 1000000000) {
    mfg::ApprovalBinding approval;
    approval.approval_id = std::move(approval_id);
    approval.run_id = std::move(run_id);
    approval.issued_at_seq = 1;
    approval.machine = {"dev:emulated:0123456789ab", "fingerprint"};
    approval.artifact = {"artifact", 42, mfg::tier0::ArtifactKind::Nc,
                         "post", "post-hash", ""};
    approval.job = {"job:one", 4, 10, 1};
    approval.snapshot = {"tools", "fixture", {"G54", {0, 0, 0}, "res:wcs"}};
    approval.preflight = {{"res:preflight"}, "res:gate", 20};
    approval.approver = {"operator", "os_seat+pin", 10};
    approval.expiry = {expiry, 1000};
    approval.exercisable_by = exercisable;
    approval.nonce = {"one-shot-nonce", mfg::NonceState::Unburned, std::nullopt};
    approval.max_starts = 1;
    approval.attendance = {mfg::AttendanceMode::PhysicalAttended, "operator", 2};
    approval.spindle_authority = mfg::SpindleAuthority::NotApplicable;
    approval.policy_revision = 7;
    return approval;
}

mfg::ApprovalInputs inputs_for(const mfg::ApprovalBinding& approval) {
    return {approval.machine, approval.artifact, approval.job, approval.snapshot,
            approval.preflight, approval.policy_revision, true};
}

mfg::RunAuthorization make_run(const mfg::ApprovalBinding& approval) {
    mfg::RunAuthorization run;
    run.run_id = approval.run_id;
    run.approval_id = approval.approval_id;
    run.state = device::RunState::Approved;
    run.current_inputs = inputs_for(approval);
    run.tier0_context.action = mfg::tier0::Action::RunStart;
    run.tier0_context.artifact_kind = mfg::tier0::ArtifactKind::Nc;
    run.tier0_context.machine_class = device::MachineClass::Emulated;
    run.tier0_context.policy = mfg::default_policy(device::MachineClass::Emulated);
    run.tier0_context.policy.max_exercisable_by = mfg::ExercisableBy::HumanOrAgent;
    run.tier0_context.policy.revision = approval.policy_revision;
    run.tier0_context.execute_capability = {
        device::CapabilityLevel::Execute, device::CapabilityTruthValue::Verified, {"res:e3"},
        device::EmulatorStage::E3, std::nullopt, {}};
    run.tier0_context.emulator_stage_passed = device::EmulatorStage::E3;
    run.tier0_context.commander_lease_held_by_caller = true;

    run.gate_input.machine_class = device::MachineClass::Emulated;
    run.gate_input.artifact_kind = mfg::tier0::ArtifactKind::Nc;
    run.gate_input.facts = passing_facts();
    run.gate_input.facts.emulator_stage_passed = device::EmulatorStage::E3;
    run.gate_input.facts.emulator_stage_required = device::EmulatorStage::E3;
    mfg::gates::Stage1Check controller;
    controller.name = "controller_snapshot";
    controller.subject = {approval.machine.device_id, "dev", 7, true, true,
                          approval.machine.device_id, device::MachineClass::Emulated};
    controller.candidate.offered_as = mfg::gates::Admission::ProbedEvidence;
    controller.candidate.fresh = {mfg::FreshState::Ok, mfg::FreshSource::Simulated, true,
                                  0, 1000, 2, "emulator", "command", {}, 1000000};
    controller.candidate.execute_truth = device::CapabilityTruthValue::Verified;
    controller.candidate.approval_device_id = approval.machine.device_id;
    controller.accepts = {{"dev"}, {mfg::gates::Admission::ProbedEvidence}};
    run.gate_input.stage1.push_back(std::move(controller));
    return run;
}

struct ArbiterRig {
    TempDirectory temp;
    mfg::Journal journal{temp.path, device::LaneEpoch::mint("authorization")};
    FakeClock clock;
    mfg::NonceLedger nonces;
    mfg::Arbiter arbiter{journal, clock, nonces};
};

}  // namespace

TEST_CASE("Tier-0 register is artifact and machine-class scoped and names refusals") {
    using namespace chudware;
    auto context = mfg::tier0::Context{};
    context.action = mfg::tier0::Action::RunStart;
    context.artifact_kind = mfg::tier0::ArtifactKind::Nc;
    context.machine_class = device::MachineClass::OpenFrameRouter;
    context.policy = mfg::default_policy(context.machine_class);
    context.execute_capability = {device::CapabilityLevel::Execute,
        device::CapabilityTruthValue::Assumed, {}, device::EmulatorStage::E3,
        device::CapabilityLevel::Execute, "physical execute requires E6"};
    context.attendance_mode = mfg::AttendanceMode::RemoteAttended;
    context.spindle_request = mfg::SpindleRequest::AutomatedControl;
    context.emulator_stage_passed = device::EmulatorStage::E3;

    const auto refusals = mfg::tier0::evaluate(context);
    REQUIRE(refusals.size() >= 4);
    CHECK(refusals[0].prerequisite == "capability.execute");
    CHECK(refusals[0].missing_level == "execute");
    CHECK(refusals[0].artifact_dependent);
    CHECK(refusals[0].why.find("assumed") != std::string::npos);
    CHECK(std::any_of(refusals.begin(), refusals.end(), [](const auto& value) {
        return value.prerequisite == "attendance.attested" &&
               value.why.find("mode_not_allowed") != std::string::npos;
    }));
    CHECK(std::any_of(refusals.begin(), refusals.end(), [](const auto& value) {
        return value.prerequisite == "spindle_authority.satisfied" &&
               value.why.find("not_commissioned") != std::string::npos;
    }));

    const auto print_rows = mfg::tier0::classify(mfg::tier0::Action::RunStart,
        mfg::tier0::ArtifactKind::PrintJob, device::MachineClass::FdmPrinterOpen);
    CHECK_FALSE(std::any_of(print_rows.begin(), print_rows.end(), [](const auto& row) {
        return row.prerequisite == "spindle_authority.satisfied";
    }));
    CHECK(std::any_of(print_rows.begin(), print_rows.end(), [](const auto& row) {
        return row.prerequisite == "bed_origin.declared_by_firmware" && row.artifact_dependent;
    }));
}

TEST_CASE("commissioning policy requires attendance and honest spindle authority") {
    auto policy = chudware::mfg::default_policy(chudware::device::MachineClass::OpenFrameMill);
    CHECK_FALSE(chudware::mfg::check_attendance(
        policy, chudware::mfg::AttendanceMode::Unattended, true).allowed);
    CHECK_FALSE(chudware::mfg::check_spindle_authority(
        policy, chudware::mfg::SpindleRequest::AutomatedControl, false, true, true).allowed);
    policy.feedback_relay_commissioned = true;
    CHECK_FALSE(chudware::mfg::check_spindle_authority(
        policy, chudware::mfg::SpindleRequest::AutomatedControl, false, true, false).allowed);
    CHECK(chudware::mfg::check_spindle_authority(
        policy, chudware::mfg::SpindleRequest::AutomatedControl, false, true, true).allowed);
    CHECK(chudware::mfg::check_spindle_authority(
        policy, chudware::mfg::SpindleRequest::ManualMotion, true, false, false).allowed);
}

TEST_CASE("Stage-1 admission fails closed and procedural facts stay bounded") {
    using namespace chudware;
    mfg::gates::SubjectRef physical{"dev:physical", "dev", 4, true, true,
                                    "dev:physical", device::MachineClass::OpenFrameRouter};
    mfg::gates::GuardAccepts probed{{"dev"}, {mfg::gates::Admission::ProbedEvidence}};
    mfg::gates::AdmissionCandidate candidate;
    candidate.offered_as = mfg::gates::Admission::ProbedEvidence;
    candidate.execute_truth = device::CapabilityTruthValue::Verified;
    candidate.approval_device_id = physical.device_id;
    candidate.fresh = {mfg::FreshState::Ok, mfg::FreshSource::Probed, true,
                       1, 10, 1, "probe", "device", {}, 0};
    CHECK(mfg::gates::admit(candidate, physical, probed).admit == mfg::gates::Admit::Admitted);

    for (const auto state : {mfg::FreshState::Stale, mfg::FreshState::Unknown}) {
        candidate.fresh.state = state;
        candidate.fresh.why = "not fresh";
        CHECK(mfg::gates::admit(candidate, physical, probed).admit == mfg::gates::Admit::Rejected);
    }
    candidate.fresh.state = mfg::FreshState::Ok;
    for (const auto source : {mfg::FreshSource::Simulated, mfg::FreshSource::Advisory,
                              mfg::FreshSource::Declared}) {
        candidate.fresh.src = source;
        CHECK(mfg::gates::admit(candidate, physical, probed).admit == mfg::gates::Admit::Rejected);
    }

    mfg::gates::SubjectRef artifact{"art:abc", "art", 9, false, false, {},
                                    device::MachineClass::Emulated};
    mfg::gates::AdmissionCandidate fact;
    fact.offered_as = mfg::gates::Admission::ProceduralFact;
    fact.procedural_fact = mfg::gates::ProceduralFact{
        "post_check", "art:abc", "hash", 9, std::nullopt, 44, "pass"};
    mfg::gates::GuardAccepts accepts{{"art"}, {mfg::gates::Admission::ProceduralFact}};
    CHECK(mfg::gates::admit(fact, artifact, accepts).admit == mfg::gates::Admit::Admitted);
    artifact.execute_side = true;
    CHECK(mfg::gates::admit(fact, artifact, accepts).why ==
          "procedural_fact_forbidden_for_execute_side_subject");
}

TEST_CASE("corroboration requires same binding and independent fresh lane") {
    using namespace chudware::mfg::gates;
    const CorroborationEvidence claim{"dev:x", 3, "controller", "lane:a", true};
    CHECK(corroborate(claim, {"dev:x", 3, "door", "lane:b", true}).corroborated);
    CHECK(corroborate(claim, {"dev:x", 4, "door", "lane:b", true}).rejecting_axis == "binding");
    CHECK(corroborate(claim, {"dev:x", 3, "controller", "lane:b", true}).rejecting_axis == "independence");
    CHECK(corroborate(claim, {"dev:x", 3, "door", "lane:a", true}).rejecting_axis == "independence");

    auto facts = passing_facts();
    facts.interlock_closed = false;
    facts.missing_interlock_claim = claim;
    facts.missing_interlock_corroborator = CorroborationEvidence{
        "dev:x", 3, "door", "lane:b", true};
    CHECK(evaluate_guard(Guard::InterlockEngaged, facts).verdict == GuardVerdict::NotApplicable);
}

TEST_CASE("every shipped Stage-2 guard has both a pass and a fail input") {
    using namespace chudware::mfg::gates;
    CHECK(all_guards.size() == 15);
    for (const auto guard : all_guards) {
        CAPTURE(to_string(guard));
        auto facts = passing_facts();
        CHECK(evaluate_guard(guard, facts).verdict == GuardVerdict::Pass);
        make_guard_fail(guard, facts);
        CHECK(evaluate_guard(guard, facts).verdict == GuardVerdict::Fail);
    }
}

TEST_CASE("gate report is seq stamped complete and not-applicable never counts as pass") {
    using namespace chudware;
    mfg::gates::GateInput input;
    input.machine_class = device::MachineClass::EnclosedInterlockedMill;
    input.artifact_kind = mfg::tier0::ArtifactKind::Nc;
    input.facts = passing_facts();
    input.facts.missing_interlock_claim = {"dev:x", 1, "claim", "a", true};
    input.facts.missing_interlock_corroborator = {"dev:x", 1, "other", "b", true};
    input.facts.interlock_closed = false;
    const auto report = mfg::gates::evaluate(input, "run:x", "res:gate_report:x", 77);
    CHECK(report.seq == 77);
    CHECK(report.ref == "res:gate_report:x");
    CHECK(report.stage2.size() == report.required_set.size());
    CHECK_FALSE(report.passed);
    CHECK(std::any_of(report.rejecting.begin(), report.rejecting.end(), [](const auto& value) {
        return value.find("interlock_engaged") != std::string::npos;
    }));
}

TEST_CASE("approval binding falsifies on fingerprint or any frozen input change") {
    const auto run_id = chudware::device::RunId::mint().str();
    const auto approval_id = chudware::device::ApprovalId::mint().str();
    const auto approval = make_approval(run_id, approval_id, chudware::mfg::ExercisableBy::HumanOnly);
    auto inputs = inputs_for(approval);
    CHECK_FALSE(chudware::mfg::falsification_against(approval, inputs, 100).has_value());
    inputs.machine.fingerprint_sha256 = "changed";
    REQUIRE(chudware::mfg::falsification_against(approval, inputs, 100).has_value());
    CHECK(chudware::mfg::falsification_against(approval, inputs, 100)->reason ==
          "machine_fingerprint_changed");
    inputs = inputs_for(approval);
    inputs.snapshot.fixture_hash = "changed";
    CHECK(chudware::mfg::falsification_against(approval, inputs, 100)->reason == "fixture_changed");
}

TEST_CASE("nonce ledger is one-shot and names replay") {
    const auto run_id = chudware::device::RunId::mint().str();
    const auto approval_id = chudware::device::ApprovalId::mint().str();
    const auto approval = make_approval(run_id, approval_id, chudware::mfg::ExercisableBy::HumanOnly);
    chudware::mfg::NonceLedger ledger;
    REQUIRE(ledger.register_approval(approval));
    CHECK(ledger.burn(approval_id, approval.nonce.value, 8).burned);
    const auto replay = ledger.burn(approval_id, approval.nonce.value, 9);
    CHECK_FALSE(replay.burned);
    CHECK(replay.code == "nonce.replay_refused");
    CHECK(replay.burned_at_seq == 8);
}

TEST_CASE("commander lease is exclusive, renewable, expiring, and takeover-safe") {
    TempDirectory temp;
    chudware::mfg::Journal journal(temp.path, chudware::device::LaneEpoch::mint("lease-test"));
    chudware::mfg::LeaseRegistry leases(journal);
    const chudware::mfg::LeaseOwner first{"gui", "operator@ws1", "one"};
    const chudware::mfg::LeaseOwner second{"agent", "agent@ws1", "two"};
    const auto claimed = leases.claim("dev:x", first, 100, 1000, 100);
    REQUIRE(claimed.ok);
    const auto refused = leases.claim("dev:x", second, 200, 1000, 100);
    CHECK_FALSE(refused.ok);
    CHECK(refused.code == "lease.takeover_refused");
    CHECK(refused.current_owner == first);
    REQUIRE(claimed.lease.has_value());
    CHECK(leases.renew(claimed.lease->lease_id, first, 300, 2000).ok);
    CHECK(leases.held_by("dev:x", first, 400));
    CHECK_FALSE(leases.claim("dev:x", second, 1000000301ULL, 1000, 100).ok);
    CHECK(leases.claim("dev:x", second, 2000000000400ULL, 1000, 100).ok);
}

TEST_CASE("lease TTL is bounded and cannot saturate monotonic expiry") {
    TempDirectory temp;
    chudware::mfg::Journal journal(temp.path, chudware::device::LaneEpoch::mint("lease-bound"));
    chudware::mfg::LeaseRegistry leases(journal);
    const chudware::mfg::LeaseOwner owner{"raw", "seat", "one"};
    CHECK_FALSE(leases.claim("dev:x", owner, 100, UINT64_MAX, 100).ok);
    const auto bounded = leases.claim("dev:x", owner, 100,
                                      chudware::mfg::CommandLease::max_ttl_ms, 100);
    REQUIRE(bounded.ok);
    CHECK(bounded.lease->expires_at_mono_ns ==
          100 + chudware::mfg::CommandLease::max_ttl_ms * 1000000ULL);
}

TEST_CASE("start-time stale telemetry rejects before nonce burn") {
    using namespace chudware;
    ArbiterRig rig;
    const auto run_id = device::RunId::mint().str();
    const auto approval_id = device::ApprovalId::mint().str();
    auto approval = make_approval(run_id, approval_id, mfg::ExercisableBy::HumanOrAgent,
                                  10'000'000'000ULL);
    auto run = make_run(approval);
    run.gate_input.stage1.front().candidate.fresh.ttl_ms = 1;
    run.gate_input.stage1.front().candidate.fresh.observed_mono_ns = rig.clock.now;
    REQUIRE(rig.arbiter.register_approval(approval));
    REQUIRE(rig.arbiter.register_run(std::move(run)));
    rig.clock.now += 2'000'000ULL;
    const auto outcome = rig.arbiter.start(
        run_id, approval_id,
        {mfg::gates::InitiatorKind::Agent, "raw", "seat", "session"});
    CHECK_FALSE(outcome.ok);
    CHECK_FALSE(outcome.nonce_burned_at_seq.has_value());
    REQUIRE(rig.nonces.describe(approval_id).has_value());
    CHECK(rig.nonces.describe(approval_id)->state == mfg::NonceState::Unburned);
}

TEST_CASE("arbiter refuses human-only agent, expiry, and fingerprint changes by name") {
    using namespace chudware;
    SUBCASE("human_only") {
        ArbiterRig rig;
        const auto run_id = device::RunId::mint().str();
        const auto approval_id = device::ApprovalId::mint().str();
        auto approval = make_approval(run_id, approval_id, mfg::ExercisableBy::HumanOnly);
        REQUIRE(rig.arbiter.register_approval(approval));
        REQUIRE(rig.arbiter.register_run(make_run(approval)));
        const auto outcome = rig.arbiter.start(run_id, approval_id,
            {mfg::gates::InitiatorKind::Agent, "planner", "agent-seat", "agent-run:1"});
        CHECK_FALSE(outcome.ok);
        CHECK(outcome.code.find("exercisable_by_permits_initiator") != std::string::npos);
    }
    SUBCASE("expired") {
        ArbiterRig rig;
        const auto run_id = device::RunId::mint().str();
        const auto approval_id = device::ApprovalId::mint().str();
        auto approval = make_approval(run_id, approval_id,
                                      mfg::ExercisableBy::HumanOrAgent, 900000);
        REQUIRE(rig.arbiter.register_approval(approval));
        REQUIRE(rig.arbiter.register_run(make_run(approval)));
        const auto outcome = rig.arbiter.start(run_id, approval_id,
            {mfg::gates::InitiatorKind::Human, "operator", "seat", std::nullopt});
        CHECK_FALSE(outcome.ok);
        CHECK(outcome.code == "tier0:approval.present_unexpired_unburned");
        CHECK(outcome.why.find("expired") != std::string::npos);
    }
    SUBCASE("fingerprint changed") {
        ArbiterRig rig;
        const auto run_id = device::RunId::mint().str();
        const auto approval_id = device::ApprovalId::mint().str();
        auto approval = make_approval(run_id, approval_id, mfg::ExercisableBy::HumanOnly);
        auto run = make_run(approval);
        run.current_inputs.machine.fingerprint_sha256 = "changed";
        REQUIRE(rig.arbiter.register_approval(approval));
        REQUIRE(rig.arbiter.register_run(std::move(run)));
        const auto outcome = rig.arbiter.start(run_id, approval_id,
            {mfg::gates::InitiatorKind::Human, "operator", "seat", std::nullopt});
        CHECK_FALSE(outcome.ok);
        CHECK(outcome.code.find("machine_fingerprint_matches") != std::string::npos);
        const auto events = rig.journal.replay();
        CHECK(std::any_of(events.begin(), events.end(), [](const auto& event) {
            return event.kind == "mfg.result.falsified" &&
                   event.body.value("reason", "") == "machine_fingerprint_changed";
        }));
    }
}

TEST_CASE("human-or-agent starts once with provenance and alarm can never transparently resume") {
    using namespace chudware;
    ArbiterRig rig;
    const auto run_id = device::RunId::mint().str();
    const auto approval_id = device::ApprovalId::mint().str();
    auto approval = make_approval(run_id, approval_id, mfg::ExercisableBy::HumanOrAgent);
    REQUIRE(rig.arbiter.register_approval(approval));
    REQUIRE(rig.arbiter.register_run(make_run(approval)));
    const mfg::Initiator agent{mfg::gates::InitiatorKind::Agent, "planner", "agent-seat", "agent-run:1"};
    const auto started = rig.arbiter.start(run_id, approval_id, agent);
    REQUIRE(started.ok);
    CHECK(started.state == device::RunState::Running);
    REQUIRE(started.nonce_burned_at_seq.has_value());
    REQUIRE(rig.arbiter.describe_run(run_id)->initiator.has_value());
    CHECK(rig.arbiter.describe_run(run_id)->initiator->kind == mfg::gates::InitiatorKind::Agent);
    CHECK(rig.arbiter.describe_run(run_id)->initiator->agent_run_id == "agent-run:1");
    const auto journal_rows = rig.journal.replay();
    const auto persisted_report = std::find_if(journal_rows.begin(), journal_rows.end(),
        [](const auto& row) { return row.kind == "mfg.run.gate_report"; });
    REQUIRE(persisted_report != journal_rows.end());
    CHECK(persisted_report->body.at("stage1").size() == 1);
    CHECK(persisted_report->body.at("stage2").size() ==
          persisted_report->body.at("required_set").size());
    CHECK(persisted_report->body.contains("waivers"));

    const auto replay = rig.arbiter.start(run_id, approval_id, agent);
    CHECK_FALSE(replay.ok);
    CHECK(replay.code == "tier0:approval.present_unexpired_unburned");
    CHECK(replay.why.find("burned") != std::string::npos);

    const auto holding = rig.arbiter.hold(run_id,
        {mfg::gates::InitiatorKind::Human, "operator", "seat", std::nullopt},
        mfg::HoldKind::FeedHold);
    REQUIRE(holding.ok);
    CHECK(holding.state == device::RunState::Holding);
    CHECK(rig.arbiter.describe_run(run_id)->state == device::RunState::Holding);
    CHECK_FALSE(rig.arbiter.resume(run_id, agent).ok);
    REQUIRE(rig.arbiter.observe_state(run_id, device::RunState::Held, "hold_observed").ok);
    REQUIRE(rig.arbiter.record_discontinuity(run_id, device::RunDiscontinuity::Alarm));
    const auto resume = rig.arbiter.resume(run_id, agent);
    CHECK_FALSE(resume.ok);
    CHECK(resume.code == "no_alarm_since_hold");
    CHECK(resume.why.find("author a new run") != std::string::npos);
}

TEST_CASE("either initiator may request hold or abort") {
    using namespace chudware;
    ArbiterRig rig;
    const auto run_id = device::RunId::mint().str();
    const auto approval_id = device::ApprovalId::mint().str();
    auto approval = make_approval(run_id, approval_id, mfg::ExercisableBy::HumanOrAgent);
    REQUIRE(rig.arbiter.register_approval(approval));
    REQUIRE(rig.arbiter.register_run(make_run(approval)));
    const mfg::Initiator human{mfg::gates::InitiatorKind::Human, "operator", "seat", std::nullopt};
    const mfg::Initiator agent{mfg::gates::InitiatorKind::Agent, "planner", "agent-seat", "agent-run:2"};
    REQUIRE(rig.arbiter.start(run_id, approval_id, human).ok);
    REQUIRE(rig.arbiter.hold(run_id, agent, mfg::HoldKind::ControlledStop).ok);
    CHECK(rig.arbiter.describe_run(run_id)->state == device::RunState::Holding);
    REQUIRE(rig.arbiter.observe_state(run_id, device::RunState::Held, "hold_observed").ok);
    REQUIRE(rig.arbiter.resume(run_id, human).ok);
    const auto aborted = rig.arbiter.abort(run_id, agent);
    CHECK(aborted.ok);
    CHECK(aborted.state == device::RunState::Aborting);
}
