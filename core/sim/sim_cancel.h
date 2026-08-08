// core/sim/sim_cancel.h — run/cancel registry for sim jobs.
//
// Mirrors core/agent/machine.cpp's agent cancel: a process-wide registry maps a job_id to a
// shared atomic flag; the running job (the LBM wind tunnel) polls its token between steps
// (lbm_run's should_cancel), and sim_cancel flips the flag. The dispatch entry (chudware_call
// in core/api/libchudware.cpp) exposes sim_cancel(job_id) as an RPC; this header is the whole
// surface the sim subsystem needs.
#pragma once

#include <atomic>
#include <memory>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware {
namespace sim {

// Shared cancel flag a running sim job polls between steps.
using SimCancelToken = std::shared_ptr<std::atomic<bool>>;

// Register a running generation and return the flag it polls. Reusing an id first
// cancels the previous generation, then installs the new token, so at most one
// uncancelled generation owns an id. Empty ids are anonymous and return nullptr.
SimCancelToken sim_job_register(const std::string& job_id);

// Remove only the generation identified by `token`. This identity check is required:
// an older run can finish after its id has been reused and must not unregister the
// newer run. Empty ids/tokens and stale generations are harmless no-ops.
void sim_job_unregister(const std::string& job_id, const SimCancelToken& token);

// The sim_cancel RPC body: {job_id} -> {ok, cancelled, job_id}. Mirrors agent::cancel_run:
// an unknown or already-finished job is NOT an error — it returns ok:true, cancelled:false
// (cancel-after-finish stays idempotent).
nlohmann::json sim_cancel(const std::string& job_id);

}  // namespace sim
}  // namespace chudware
