#pragma once

#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>

#include "device/controller_snapshot.h"
#include "manufacturing/approval.h"
#include "manufacturing/gates.h"
#include "manufacturing/journal.h"
#include "manufacturing/tier0.h"

namespace chudware::mfg {

class Clock {
public:
    virtual ~Clock() = default;
    virtual uint64_t mono_ns() const = 0;
};

enum class HoldKind { FeedHold, ControlledStop, DoorSuspend };

struct Initiator {
    gates::InitiatorKind kind = gates::InitiatorKind::Human;
    std::string identity;
    std::string seat;
    std::optional<std::string> agent_run_id;
};

struct RunAuthorization {
    std::string run_id;
    std::string approval_id;
    device::RunState state = device::RunState::Approved;
    tier0::Context tier0_context;
    gates::GateInput gate_input;
    ApprovalInputs current_inputs;
    device::RunDiscontinuity discontinuity_since_hold = device::RunDiscontinuity::None;
    std::optional<InitiatorProvenance> initiator;
    std::optional<std::string> gate_report_ref;
};

struct StartOutcome {
    bool ok = false;
    std::string code;
    std::string why;
    device::RunState state = device::RunState::Draft;
    std::optional<device::Seq> at_seq;
    std::optional<device::Seq> nonce_burned_at_seq;
    std::optional<gates::GateReport> gate_report;
};

struct RunOutcome {
    bool ok = false;
    std::string code;
    std::string why;
    device::RunState state = device::RunState::Draft;
    std::optional<device::Seq> at_seq;
};

class Arbiter {
public:
    Arbiter(Journal& journal, Clock& clock, NonceLedger& nonces)
        : journal_(journal), clock_(clock), nonces_(nonces) {}

    bool register_approval(ApprovalBinding approval, std::string* why = nullptr);
    bool register_run(RunAuthorization run, std::string* why = nullptr);
    bool replace_current_inputs(std::string_view run_id, ApprovalInputs current,
                                std::string* why = nullptr);

    StartOutcome start(std::string_view run_id, std::string_view approval_id,
                       const Initiator& initiator);
    RunOutcome hold(std::string_view run_id, const Initiator& initiator, HoldKind kind);
    RunOutcome abort(std::string_view run_id, const Initiator& initiator);
    RunOutcome resume(std::string_view run_id, const Initiator& initiator);
    bool record_discontinuity(std::string_view run_id, device::RunDiscontinuity discontinuity);

    std::optional<gates::GateReport> evaluate(std::string_view run_id,
                                              const Initiator& initiator) const;
    std::optional<RunAuthorization> describe_run(std::string_view run_id) const;
    std::optional<ApprovalBinding> describe_approval(std::string_view approval_id) const;

private:
    gates::GateInput materialize_gate_input(const RunAuthorization& run,
                                            const ApprovalBinding& approval,
                                            const Initiator& initiator) const;
    tier0::Context materialize_tier0(const RunAuthorization& run,
                                     const ApprovalBinding& approval) const;
    bool transition(RunAuthorization& run, device::RunState to,
                    std::string_view cause, device::Seq* at_seq = nullptr);
    device::Seq journal_attempt(std::string_view action, std::string_view run_id,
                                const Initiator& initiator, std::string_view outcome,
                                std::string_view reason = {});

    Journal& journal_;
    Clock& clock_;
    NonceLedger& nonces_;
    mutable std::mutex mutex_;
    std::map<std::string, ApprovalBinding, std::less<>> approvals_;
    std::map<std::string, RunAuthorization, std::less<>> runs_;
};

}  // namespace chudware::mfg
