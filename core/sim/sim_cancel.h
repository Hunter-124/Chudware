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

// Register a running sim job so sim_cancel(job_id) can find it. Returns the token the job
// polls; re-registering an id supersedes the old run's token. An empty job_id is an
// anonymous run: returns nullptr and registers nothing (not cancellable).
SimCancelToken sim_job_register(const std::string& job_id);
void sim_job_unregister(const std::string& job_id);

// The sim_cancel RPC body: {job_id} -> {ok, cancelled, job_id}. Mirrors agent::cancel_run:
// an unknown or already-finished job is NOT an error — it returns ok:true, cancelled:false
// (cancel-after-finish stays idempotent).
nlohmann::json sim_cancel(const std::string& job_id);

}  // namespace sim
}  // namespace chudware
