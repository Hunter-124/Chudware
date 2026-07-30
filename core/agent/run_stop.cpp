// core/agent/run_stop.cpp — how a general-work turn whose python worker never
// answered is reported to the user.
//
// This lives in its OWN translation unit on purpose. The rule belongs to run.cpp,
// but run.cpp pulls in the whole chudware::doc::* session API, and the agent tests
// (core/tests/test_agent.cpp) deliberately link only `chudware_agent` — no doc, no
// OCCT. Defining the rule here lets that test LINK the shipped implementation
// instead of restating it in a mirrored lambda, which is how the older stop_reason
// cases in test_agent.cpp were written and exactly how a rule and its test drift.

#include "agent/run.h"

namespace chudware {
namespace agent {

WorkerStop classify_worker_stop(bool crashed, bool timed_out, bool reached_worker) {
    // A crash and a silence are BOTH Chudware failing, not the user asking for
    // something unrunnable, so both get status "failed" and their own stop_reason.
    // `crashed` wins when both are set: a worker killed on the timeout path still
    // reports a signal, and the crash is the more specific fact.
    if (crashed) return {"worker_crash", "failed", true};
    if (timed_out) return {"worker_timeout", "failed", true};
    // The offline pre-flight is the ONLY path that never reaches the worker, so it is
    // the only one that can honestly promise the model is untouched.
    return {"non_modeling_request", "blocked", reached_worker};
}

}  // namespace agent
}  // namespace chudware
