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
#include <stdexcept>
#include <optional>
#include <cstdint>

#include <nlohmann/json.hpp>

namespace chudware {
namespace sim {

// Shared cancel flag a running sim job polls between steps.
using SimCancelToken = std::shared_ptr<std::atomic<bool>>;

// Trusted native dispatch context. Missing context retains desktop behavior.
// Nested in-process calls inherit the caller; scopes restore it on every exit.
struct CellPrincipal {
    std::string workspaceKey;
    std::string actorKey;
    bool manager = false;
    std::int64_t epoch = 0;
};
class CellOwnershipError : public std::runtime_error {
public:
    CellOwnershipError() : std::runtime_error("not_found") {}
};
class CellRevokedError : public std::runtime_error {
public:
    CellRevokedError() : std::runtime_error("revoked") {}
};
const CellPrincipal* current_cell_principal();
// Preserve the canonical denial when a solver catches a registry exception.
bool cell_request_revoked();
class CellRequestScope {
public:
    explicit CellRequestScope(const CellPrincipal* principal);
    ~CellRequestScope();
    CellRequestScope(const CellRequestScope&) = delete;
    CellRequestScope& operator=(const CellRequestScope&) = delete;
private:
    const CellPrincipal* previous_;
    bool previousRevoked_;
};

// Register a running generation, stamping the current cell principal. Reusing
// an owned id cancels its previous generation; foreign id reuse throws not_found.
// Desktop empty ids remain anonymous/null; cell anonymous jobs get owned tokens.
SimCancelToken sim_job_register(const std::string& job_id);

// Remove only the generation identified by `token`. This identity check is required:
// an older run can finish after its id has been reused and must not unregister the
// newer run. Empty ids/tokens and stale generations are harmless no-ops.
void sim_job_unregister(const std::string& job_id, const SimCancelToken& token);

// The sim_cancel RPC body: {job_id} -> {ok, cancelled, job_id}. Desktop misses
// stay idempotent and empty ids are a no-op. Cell misses/foreign jobs return
// not_found; a manager can cancel named workspace jobs; empty means own jobs only.
nlohmann::json sim_cancel(const std::string& job_id,
                          std::optional<std::int64_t> revokeThrough = std::nullopt);

}  // namespace sim
}  // namespace chudware
