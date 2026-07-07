// PLM Project Hub — async state fetchers + cache invalidation.
//
// Every page reads only the cached rows on g_plmHub; these helpers refill them
// via st.rpc (async, drained on the GL thread). A mutation bumps
// g_plmHub.generation; each ensureX refetches when its cached generation is stale.
// All calls pass {"path": st.plmProjectPath()} — the vault worker is path-addressed.
#include "plm_hub.h"

#include <string>

#include "core/app_state.h"
#include "core/async_rpc.h"

namespace ui {
namespace {

nlohmann::json pathParam(AppState& st) {
    nlohmann::json p;
    p["path"] = st.plmProjectPath();
    return p;
}

std::string getStr(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return "";
    if (it->is_string()) return it->get<std::string>();
    return it->dump();
}

std::string getActor(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return "";
    if (it->is_object()) return getStr(*it, "display");
    if (it->is_string()) return it->get<std::string>();
    return "";
}

PlmItemRow parseItem(const nlohmann::json& it) {
    PlmItemRow r;
    r.itemId     = getStr(it, "item_id");
    r.path       = getStr(it, "path");
    r.kind       = getStr(it, "kind");
    r.docType    = getStr(it, "doc_type");
    r.partNumber = getStr(it, "part_number");
    r.rev        = getStr(it, "rev");
    r.lifecycle  = getStr(it, "lifecycle");
    r.workingRev = getStr(it, "working_rev");
    r.dirty      = it.value("dirty", false);
    return r;
}

PlmEntryRow parseEntry(const nlohmann::json& e) {
    PlmEntryRow r;
    r.commit     = getStr(e, "commit");
    r.kind       = getStr(e, "kind");
    r.scope      = getStr(e, "scope");
    r.authoredAt = getStr(e, "authored_at");
    r.author     = getActor(e, "author");
    r.message    = getStr(e, "message");
    r.summary    = getStr(e, "summary");
    return r;
}

PlmTaskRow parseTask(const nlohmann::json& t) {
    PlmTaskRow r;
    r.id          = getStr(t, "id");
    r.title       = getStr(t, "title");
    r.status      = getStr(t, "status");
    r.priority    = getStr(t, "priority");
    r.assignee    = getActor(t, "assignee");
    r.milestoneId = getStr(t, "milestone_id");
    return r;
}

PlmIssueRow parseIssue(const nlohmann::json& i) {
    PlmIssueRow r;
    r.id       = getStr(i, "id");
    r.title    = getStr(i, "title");
    r.severity = getStr(i, "severity");
    r.state    = getStr(i, "state");
    r.assignee = getActor(i, "assignee");
    return r;
}

PlmMilestoneRow parseMilestone(const nlohmann::json& m) {
    PlmMilestoneRow r;
    r.id     = getStr(m, "id");
    r.title  = getStr(m, "title");
    r.status = getStr(m, "status");
    r.due    = getStr(m, "due");
    return r;
}

}  // namespace

void plmInvalidate() { g_plmHub.generation++; }

std::string plmProjectName(AppState& st) {
    std::string p = st.plmProjectPath();
    if (p.empty()) return "";
    while (!p.empty() && (p.back() == '/' || p.back() == '\\')) p.pop_back();
    size_t s = p.find_last_of("/\\");
    return s == std::string::npos ? p : p.substr(s + 1);
}

void plmEnsureOverview(AppState& st) {
    auto& H = g_plmHub;
    if (H.ovLoading || H.ovGen == H.generation) return;
    H.ovLoading = true;
    int gen = H.generation;
    st.rpc.call("plm_overview", pathParam(st), [gen](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.ovLoading = false;
        H.ovGen = gen;
        if (!r.ok) { H.ovErr = r.err; return; }
        H.ovErr.clear();
        PlmOverview ov;
        const auto& res = r.result;
        if (auto c = res.find("counts"); c != res.end() && c->is_object()) {
            ov.itemsCount     = c->value("items", 0);
            ov.dirtyCount     = c->value("dirty", 0);
            ov.openTasks      = c->value("open_tasks", 0);
            ov.openIssues     = c->value("open_issues", 0);
            ov.openMilestones = c->value("open_milestones", 0);
        }
        ov.aiMode = getStr(res, "ai_mode");
        if (auto d = res.find("dirty_items"); d != res.end() && d->is_array())
            for (const auto& it : *d) ov.dirtyItems.push_back(parseItem(it));
        if (auto e = res.find("recent_entries"); e != res.end() && e->is_array())
            for (const auto& en : *e) ov.recent.push_back(parseEntry(en));
        if (auto t = res.find("open_tasks"); t != res.end() && t->is_array())
            for (const auto& tk : *t) ov.tasks.push_back(parseTask(tk));
        if (auto m = res.find("next_milestone"); m != res.end() && m->is_object())
            ov.nextMilestone = getStr(*m, "title");
        H.overview = std::move(ov);
    });
}

void plmEnsureItems(AppState& st) {
    auto& H = g_plmHub;
    if (H.itemsLoading || H.itemsGen == H.generation) return;
    H.itemsLoading = true;
    int gen = H.generation;
    st.rpc.call("plm_item_list", pathParam(st), [gen](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.itemsLoading = false;
        H.itemsGen = gen;
        if (!r.ok) { H.itemsErr = r.err; return; }
        H.itemsErr.clear();
        H.items.clear();
        if (auto it = r.result.find("items"); it != r.result.end() && it->is_array())
            for (const auto& row : *it) H.items.push_back(parseItem(row));
    });
}

void plmEnsureHistory(AppState& st) {
    auto& H = g_plmHub;
    if (H.histLoading || H.histGen == H.generation) return;
    H.histLoading = true;
    int gen = H.generation;
    nlohmann::json p = pathParam(st);
    p["limit"] = 100;
    p["scope"] = (H.histScope == 1) ? "planning" : (H.histScope == 2) ? "all" : "design";
    st.rpc.call("plm_history", p, [gen](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.histLoading = false;
        H.histGen = gen;
        if (!r.ok) { H.histErr = r.err; return; }
        H.histErr.clear();
        H.history.clear();
        if (auto e = r.result.find("entries"); e != r.result.end() && e->is_array())
            for (const auto& en : *e) H.history.push_back(parseEntry(en));
    });
}

void plmEnsurePlanning(AppState& st) {
    auto& H = g_plmHub;
    if (H.planLoading || H.planGen == H.generation) return;
    H.planLoading = true;
    H.planGen = H.generation;  // fire all three; each writes its own vector
    auto call = [&](const char* entity, int slot) {
        nlohmann::json p = pathParam(st);
        p["op"] = "list";
        p["entity"] = entity;
        st.rpc.call("plm_plan", p, [slot](const core::RpcResult& r) {
            auto& H = g_plmHub;
            if (r.ok) {
                if (auto it = r.result.find("items"); it != r.result.end() && it->is_array()) {
                    if (slot == 0) { H.allTasks.clear();
                        for (const auto& t : *it) H.allTasks.push_back(parseTask(t)); }
                    else if (slot == 1) { H.issues.clear();
                        for (const auto& i : *it) H.issues.push_back(parseIssue(i)); }
                    else { H.milestones.clear();
                        for (const auto& m : *it) H.milestones.push_back(parseMilestone(m)); }
                }
            } else {
                H.planErr = r.err;
            }
            H.planLoading = false;  // any completion clears the spinner
        });
    };
    call("task", 0);
    call("issue", 1);
    call("milestone", 2);
}

void plmEnsureSettings(AppState& st) {
    auto& H = g_plmHub;
    if (H.setLoading || H.setGen == H.generation) return;
    H.setLoading = true;
    int gen = H.generation;
    st.rpc.call("plm_status", pathParam(st), [gen](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.setLoading = false;
        H.setGen = gen;
        if (!r.ok) { H.setErr = r.err; return; }
        H.setErr.clear();
        H.partPrefix = getStr(r.result, "part_number_prefix");
        H.aiMode     = getStr(r.result, "ai_mode");
    });
}

}  // namespace ui
