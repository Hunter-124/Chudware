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

PlmGitFileRow parseGitFile(const nlohmann::json& f) {
    PlmGitFileRow r;
    r.path      = getStr(f, "path");
    r.xy        = getStr(f, "xy");
    r.staged    = f.value("staged", false);
    r.unstaged  = f.value("unstaged", false);
    r.untracked = f.value("untracked", false);
    return r;
}

PlmGitCommitRow parseGitCommit(const nlohmann::json& c) {
    PlmGitCommitRow r;
    r.hash    = getStr(c, "hash");
    r.author  = getStr(c, "author");
    r.date    = getStr(c, "date");
    r.subject = getStr(c, "subject");
    return r;
}

PlmGitRefRow parseGitRef(const nlohmann::json& x) {
    PlmGitRefRow r;
    r.number = x.value("number", 0);
    r.title  = getStr(x, "title");
    r.state  = getStr(x, "state");
    r.author = getStr(x, "author");
    r.branch = getStr(x, "branch");
    return r;
}

}  // namespace

bool plmOk(const core::RpcResult& r, std::string& hint) {
    if (!r.ok) {
        hint = r.err.empty() ? "the service did not respond" : r.err;
        return false;
    }
    if (r.result.is_object() && !r.result.value("ok", true)) {
        std::string reason = r.result.value("reason", std::string());
        hint = r.result.value("hint", reason.empty() ? std::string("that could not be done")
                                                      : reason);
        return false;
    }
    return true;
}

std::string plmReason(const core::RpcResult& r) {
    if (r.ok && r.result.is_object() && !r.result.value("ok", true))
        return r.result.value("reason", std::string());
    return "";
}

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
    // Hub-open drift capture: when the overview first binds a project folder,
    // record on-disk changes made outside the editor. Complements
    // setActiveProjectDir (which already fires this on explicit folder pick).
    // Idempotent per projectDir; no-op when the working tree matches history.
    {
        std::string proj = st.plmProjectPath();
        if (!proj.empty() && proj != H.outsideRecordedFor && st.workerStarted) {
            H.outsideRecordedFor = proj;
            nlohmann::json p;
            p["path"] = proj;
            st.rpc.call("plm_record_outside", p, [](const core::RpcResult&) {});
        }
    }
    if (H.ovLoading || H.ovGen == H.generation) return;
    H.ovLoading = true;
    int gen = H.generation;
    st.rpc.call("plm_overview", pathParam(st), [gen](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.ovLoading = false;
        H.ovGen = gen;
        if (!plmOk(r, H.ovErr)) return;
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
        if (!plmOk(r, H.itemsErr)) return;
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
        if (!plmOk(r, H.histErr)) return;
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
            std::string h;
            if (plmOk(r, h)) {
                if (auto it = r.result.find("items"); it != r.result.end() && it->is_array()) {
                    if (slot == 0) { H.allTasks.clear();
                        for (const auto& t : *it) H.allTasks.push_back(parseTask(t)); }
                    else if (slot == 1) { H.issues.clear();
                        for (const auto& i : *it) H.issues.push_back(parseIssue(i)); }
                    else { H.milestones.clear();
                        for (const auto& m : *it) H.milestones.push_back(parseMilestone(m)); }
                }
            } else {
                H.planErr = h;
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
        if (!plmOk(r, H.setErr)) return;
        H.setErr.clear();
        H.partPrefix = getStr(r.result, "part_number_prefix");
        H.aiMode     = getStr(r.result, "ai_mode");
        if (auto it = r.result.find("auto_commit");
            it != r.result.end() && it->is_boolean())
            H.autoCommit = it->get<bool>();
    });
}

void plmEnsureGit(AppState& st) {
    auto& H = g_plmHub;
    if (H.gitLoading || H.gitGen == H.generation) return;
    H.gitLoading = true;
    H.gitGen = H.generation;  // fan out; each callback fills its own slice

    // git status — branch, ahead/behind, changed files, is-repo/missing state.
    st.rpc.call("git_status", pathParam(st), [](const core::RpcResult& r) {
        auto& H = g_plmHub;
        std::string h;
        if (plmOk(r, h)) {
            H.gitIsRepo = true; H.gitMissing = false; H.gitErr.clear();
            H.gitBranch   = getStr(r.result, "branch");
            H.gitUpstream = getStr(r.result, "upstream");
            H.gitAhead    = r.result.value("ahead", 0);
            H.gitBehind   = r.result.value("behind", 0);
            H.gitClean    = r.result.value("clean", true);
            H.gitFiles.clear();
            if (auto f = r.result.find("files"); f != r.result.end() && f->is_array())
                for (const auto& row : *f) H.gitFiles.push_back(parseGitFile(row));
        } else {
            std::string reason = plmReason(r);
            H.gitMissing = (reason == "git_missing");
            H.gitIsRepo  = false;
            H.gitErr     = h;
        }
        H.gitLoading = false;
    });

    st.rpc.call("git_log", [&]{ nlohmann::json p = pathParam(st); p["limit"] = 60; return p; }(),
                [](const core::RpcResult& r) {
        auto& H = g_plmHub; std::string h;
        if (!plmOk(r, h)) return;
        H.gitLog.clear();
        if (auto c = r.result.find("commits"); c != r.result.end() && c->is_array())
            for (const auto& row : *c) H.gitLog.push_back(parseGitCommit(row));
        H.gitTotalCommits = r.result.value("total_commits", 0);
        H.gitCommits24h   = r.result.value("commits_24h", 0);
    });

    st.rpc.call("git_branches", pathParam(st), [](const core::RpcResult& r) {
        auto& H = g_plmHub; std::string h;
        if (!plmOk(r, h)) return;
        H.gitBranches.clear();
        if (auto b = r.result.find("branches"); b != r.result.end() && b->is_array())
            for (const auto& row : *b) {
                PlmGitBranchRow br;
                br.name    = getStr(row, "name");
                br.current = row.value("current", false);
                br.remote  = row.value("remote", false);
                H.gitBranches.push_back(br);
            }
    });

    st.rpc.call("git_remotes", pathParam(st), [](const core::RpcResult& r) {
        auto& H = g_plmHub; std::string h;
        if (!plmOk(r, h)) return;
        H.gitRemotes.clear();
        if (auto rm = r.result.find("remotes"); rm != r.result.end() && rm->is_array())
            for (const auto& row : *rm) {
                PlmGitRemoteRow re;
                re.name = getStr(row, "name");
                re.url  = getStr(row, "url");
                H.gitRemotes.push_back(re);
            }
    });

    st.rpc.call("git_tree", pathParam(st), [](const core::RpcResult& r) {
        auto& H = g_plmHub; std::string h;
        if (!plmOk(r, h)) return;
        H.gitTree.clear();
        if (auto f = r.result.find("files"); f != r.result.end() && f->is_array())
            for (const auto& row : *f) if (row.is_string()) H.gitTree.push_back(row.get<std::string>());
    });

    // GitHub side (gh) — auth, repo, PRs, issues. All degrade quietly.
    st.rpc.call("git_gh_auth_status", pathParam(st), [](const core::RpcResult& r) {
        auto& H = g_plmHub; std::string h;
        if (!plmOk(r, h)) { H.ghAuthed = false; return; }
        H.ghAuthed = r.result.value("authed", false);
        H.ghUser   = getStr(r.result, "user");
    });

    st.rpc.call("git_repo_info", pathParam(st), [](const core::RpcResult& r) {
        auto& H = g_plmHub; std::string h;
        if (!plmOk(r, h)) { H.ghRepoName.clear(); return; }
        H.ghRepoName  = getStr(r.result, "name");
        H.ghRepoOwner = getStr(r.result, "owner");
        H.ghRepoVis   = getStr(r.result, "visibility");
        H.ghRepoUrl   = getStr(r.result, "url");
    });

    st.rpc.call("git_pr_list", pathParam(st), [](const core::RpcResult& r) {
        auto& H = g_plmHub; std::string h;
        if (!plmOk(r, h)) { H.gitPrs.clear(); return; }
        H.gitPrs.clear();
        if (auto p = r.result.find("prs"); p != r.result.end() && p->is_array())
            for (const auto& row : *p) H.gitPrs.push_back(parseGitRef(row));
    });

    st.rpc.call("git_issue_list", pathParam(st), [](const core::RpcResult& r) {
        auto& H = g_plmHub; std::string h;
        if (!plmOk(r, h)) { H.gitIssues.clear(); return; }
        H.gitIssues.clear();
        if (auto i = r.result.find("issues"); i != r.result.end() && i->is_array())
            for (const auto& row : *i) H.gitIssues.push_back(parseGitRef(row));
    });
}

}  // namespace ui
