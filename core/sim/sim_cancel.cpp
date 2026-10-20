// core/sim/sim_cancel.cpp — see sim_cancel.h. Mirrors core/agent/machine.cpp's registry.
#include "sim/sim_cancel.h"

#include <algorithm>
#include <map>
#include <mutex>

namespace chudware {
namespace sim {

namespace {
std::mutex g_sim_jobs_mtx;
struct SimOwner {
    std::string workspaceKey;
    std::string actorKey;
    std::int64_t epoch = 0;
};
struct SimJob {
    SimCancelToken token;
    SimOwner owner;
};
std::map<std::string, SimJob> g_sim_jobs;
std::map<SimCancelToken, SimOwner> g_anonymous_jobs;
std::map<std::string, std::map<std::string, std::int64_t>> g_revoke_through;
thread_local const CellPrincipal* t_principal = nullptr;
thread_local bool t_revoked = false;

bool owns(const SimOwner& owner, bool allowManager) {
    return !t_principal ||
        (owner.workspaceKey == t_principal->workspaceKey &&
         (owner.actorKey == t_principal->actorKey || (allowManager && t_principal->manager)));
}

void requireLiveLocked() {
    if (!t_principal) return;
    auto workspace = g_revoke_through.find(t_principal->workspaceKey);
    if (workspace == g_revoke_through.end()) return;
    auto actor = workspace->second.find(t_principal->actorKey);
    if (actor != workspace->second.end() && t_principal->epoch <= actor->second) {
        t_revoked = true;
        throw CellRevokedError();
    }
}
}  // namespace

const CellPrincipal* current_cell_principal() { return t_principal; }
bool cell_request_revoked() { return t_principal && t_revoked; }
CellRequestScope::CellRequestScope(const CellPrincipal* principal)
    : previous_(t_principal), previousRevoked_(t_revoked) {
    if (principal != previous_) t_revoked = false;
    t_principal = principal;
}
CellRequestScope::~CellRequestScope() {
    t_principal = previous_;
    t_revoked = previousRevoked_;
}

SimCancelToken sim_job_register(const std::string& job_id) {
    std::lock_guard<std::mutex> lk(g_sim_jobs_mtx);
    requireLiveLocked();
    if (job_id.empty() && !t_principal) return nullptr;
    auto it = g_sim_jobs.find(job_id);
    if (it != g_sim_jobs.end() && !owns(it->second.owner, false)) throw CellOwnershipError();
    auto tok = std::make_shared<std::atomic<bool>>(false);
    SimOwner owner{t_principal ? t_principal->workspaceKey : std::string(),
                   t_principal ? t_principal->actorKey : std::string(),
                   t_principal ? t_principal->epoch : 0};
    if (job_id.empty()) {
        g_anonymous_jobs.emplace(tok, std::move(owner));
        return tok;
    }
    SimJob job{tok, std::move(owner)};
    if (it != g_sim_jobs.end()) {
        // Reuse cannot leave two uncancelled generations addressable by one id.
        it->second.token->store(true, std::memory_order_release);
        it->second = std::move(job);
    } else {
        g_sim_jobs.emplace(job_id, std::move(job));
    }
    return tok;
}

void sim_job_unregister(const std::string& job_id, const SimCancelToken& token) {
    if (!token) return;
    std::lock_guard<std::mutex> lk(g_sim_jobs_mtx);
    if (job_id.empty()) {
        auto it = g_anonymous_jobs.find(token);
        if (it != g_anonymous_jobs.end() && owns(it->second, false))
            g_anonymous_jobs.erase(it);
        return;
    }
    auto it = g_sim_jobs.find(job_id);
    if (it != g_sim_jobs.end() && it->second.token == token && owns(it->second.owner, false))
        g_sim_jobs.erase(it);
}

nlohmann::json sim_cancel(const std::string& job_id, std::optional<std::int64_t> revokeThrough) {
    std::lock_guard<std::mutex> lk(g_sim_jobs_mtx);
    if (job_id.empty() && t_principal) {
        if (revokeThrough) {
            auto& actors = g_revoke_through[t_principal->workspaceKey];
            auto [it, inserted] = actors.emplace(t_principal->actorKey, *revokeThrough);
            if (!inserted) it->second = std::max(it->second, *revokeThrough);
        }
        int count = 0;
        for (auto& [id, job] : g_sim_jobs) {
            // Even managers' empty-id cancellation is restricted to their own jobs.
            if (job.owner.workspaceKey != t_principal->workspaceKey ||
                job.owner.actorKey != t_principal->actorKey ||
                (revokeThrough && job.owner.epoch > *revokeThrough)) continue;
            job.token->store(true, std::memory_order_release);
            ++count;
        }
        for (auto& [token, owner] : g_anonymous_jobs) {
            if (owner.workspaceKey != t_principal->workspaceKey ||
                owner.actorKey != t_principal->actorKey ||
                (revokeThrough && owner.epoch > *revokeThrough)) continue;
            token->store(true, std::memory_order_release);
            ++count;
        }
        return nlohmann::json{{"ok", true}, {"cancelled", count != 0}, {"count", count}};
    }
    auto it = g_sim_jobs.find(job_id);
    if (t_principal && (it == g_sim_jobs.end() || !owns(it->second.owner, true)))
        return nlohmann::json{{"ok", false}, {"code", "not_found"}, {"message", "job not found"}};
    if (it == g_sim_jobs.end())
        return nlohmann::json{{"ok", true}, {"cancelled", false}, {"job_id", job_id}};
    it->second.token->store(true, std::memory_order_release);
    return nlohmann::json{{"ok", true}, {"cancelled", true}, {"job_id", job_id}};
}

}  // namespace sim
}  // namespace chudware
