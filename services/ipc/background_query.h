// Document-free GUI background queries, shared by EngineClient (which gives them a private
// per-thread hostd connection) and AsyncRpc (which runs them on their own dispatch lane), so a
// slow provider/worker status call never queues a document call behind it.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware_ipc {

inline bool isBackgroundQuery(const std::string& method, const nlohmann::json& params) {
    if (params.is_object() && params.contains("doc_id")) return false;
    // Server::clientLoop dispatches synchronously: sharing one socket would still
    // queue the GUI behind cold worker/provider calls even without a client mutex.
    // These exact names are document-free catalogs/status (worker_rpc.py's
    // subscription, OMP, skills and prompt handlers; fw_projects/status and
    // engine capability queries). Do NOT generalize to prefixes: unknown methods,
    // mints, document reads/edits and all registry/hold/subscription RPCs retain the
    // primary client identity, including its ro/CAS and edit-hold semantics.
    // omp_login is the one long call here (a whole browser OAuth flow, minutes): on the
    // primary socket it would park every other primary-socket call for that long.
    static constexpr const char* queries[] = {
        "ping", "daemon_status", "worker_control_info", "agent_list_models",
        "subscription_accounts", "subscription_status", "subscription_usage",
        "omp_provider_list", "omp_auth_state", "omp_logout_accounts", "omp_models_list",
        "omp_login", "omp_session_stats", "omp_config_get", "skills_list", "skills_get",
        "ai_prompt_baseline", "ai_tool_catalog", "fw_projects", "fw_status",
        "em_status", "sim_caps", "topology_capabilities", "topology_dfam"
    };
    for (const char* name : queries)
        if (method == name) return true;
    return false;
}

}  // namespace chudware_ipc
