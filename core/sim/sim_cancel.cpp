// core/sim/sim_cancel.cpp — see sim_cancel.h. Mirrors core/agent/machine.cpp's registry.
#include "sim/sim_cancel.h"

#include <map>
#include <mutex>

namespace chudware {
namespace sim {

namespace {
std::mutex g_sim_jobs_mtx;
std::map<std::string, SimCancelToken> g_sim_jobs;
}  // namespace

SimCancelToken sim_job_register(const std::string& job_id) {
    if (job_id.empty()) return nullptr;
    auto tok = std::make_shared<std::atomic<bool>>(false);
    std::lock_guard<std::mutex> lk(g_sim_jobs_mtx);
    g_sim_jobs[job_id] = tok;
    return tok;
}

void sim_job_unregister(const std::string& job_id) {
    if (job_id.empty()) return;
    std::lock_guard<std::mutex> lk(g_sim_jobs_mtx);
    g_sim_jobs.erase(job_id);
}

nlohmann::json sim_cancel(const std::string& job_id) {
    SimCancelToken tok;
    if (!job_id.empty()) {
        std::lock_guard<std::mutex> lk(g_sim_jobs_mtx);
        auto it = g_sim_jobs.find(job_id);
        if (it != g_sim_jobs.end()) tok = it->second;
    }
    if (!tok)
        return nlohmann::json{{"ok", true}, {"cancelled", false}, {"job_id", job_id}};
    tok->store(true);
    return nlohmann::json{{"ok", true}, {"cancelled", true}, {"job_id", job_id}};
}

}  // namespace sim
}  // namespace chudware
