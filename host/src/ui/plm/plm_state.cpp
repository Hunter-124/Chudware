// PLM Project Hub — async state fetchers + cache invalidation.
//
// Every page reads only the cached rows on g_plmHub; these helpers refill them
// via st.rpc (async, drained on the GL thread). A mutation bumps
// g_plmHub.generation; each ensureX refetches when its cached generation is stale.
// All calls pass {"path": st.plmProjectPath()} — the vault worker is path-addressed.
#include "plm_hub.h"
#include <algorithm>
#include <cstdio>
#include <ctime>
#include <string>
#include <unordered_map>
#include <vector>

#include "imgui.h"

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
    if (auto a = e.find("author"); a != e.end() && a->is_object())
        r.authorKind = getStr(*a, "kind");        // "user" | "ai" (history.py §3.4)
    r.message    = getStr(e, "message");
    r.summary    = getStr(e, "summary");
    return r;
}

PlmVersionNodeRow parseVersionNode(const nlohmann::json& e) {
    PlmVersionNodeRow r;
    r.commit       = getStr(e, "commit");
    r.kind         = getStr(e, "kind");
    r.scope        = getStr(e, "scope");
    r.authoredAt   = getStr(e, "authored_at");
    r.message      = getStr(e, "message");
    r.summary      = getStr(e, "summary");
    r.thumbnailRef = getStr(e, "thumbnail_ref");
    r.pin           = e.value("pin", false);
    if (auto p = e.find("parents"); p != e.end() && p->is_array())
        for (const auto& parent : *p)
            if (parent.is_string()) r.parents.push_back(parent.get<std::string>());
    if (auto a = e.find("author"); a != e.end() && a->is_object()) {
        r.author.kind = getStr(*a, "kind");
        r.author.id = getStr(*a, "id");
        r.author.display = getStr(*a, "display");
    }
    if (auto a = e.find("ai"); a != e.end() && a->is_object()) {
        r.ai.provider = getStr(*a, "provider");
        r.ai.model = getStr(*a, "model");
        r.ai.runId = getStr(*a, "run_id");
    }
    if (auto refs = e.find("refs"); refs != e.end() && refs->is_array())
        for (const auto& ref : *refs) {
            if (!ref.is_object()) continue;
            r.refs.push_back({getStr(ref, "type"), getStr(ref, "name")});
        }
    if (auto release = e.find("release"); release != e.end() && release->is_object()) {
        r.hasRelease = true;
        r.release.itemId = getStr(*release, "item_id");
        r.release.rev = getStr(*release, "rev");
    }
    return r;
}

// A release is two commits: the release itself, then a manifest back-fill that pins the rev
// to its commit (contract plm.md Appendix C.4). Showing both puts the same release on the
// graph twice, so the pin is folded into its parent: the pin's decorations (the branch and
// HEAD chips, since the pin is usually the tip) move onto the release node, and anything
// that pointed at the pin is re-parented to what the pin pointed at.
void collapseReleasePins(std::vector<PlmVersionNodeRow>& nodes) {
    std::unordered_map<std::string, std::vector<std::string>> pinParents;
    for (const auto& n : nodes)
        if (n.pin && !n.parents.empty()) pinParents[n.commit] = n.parents;
    for (auto& n : nodes) {
        if (n.pin) continue;
        std::vector<std::string> rewritten;
        for (const auto& parent : n.parents) {
            auto it = pinParents.find(parent);
            if (it == pinParents.end()) { rewritten.push_back(parent); continue; }
            for (const auto& grand : it->second)
                if (std::find(rewritten.begin(), rewritten.end(), grand) == rewritten.end())
                    rewritten.push_back(grand);
        }
        n.parents = std::move(rewritten);
    }
    // Carry each pin's refs onto the release commit it pinned before dropping it.
    for (const auto& n : nodes) {
        if (!n.pin || n.refs.empty()) continue;
        for (auto& target : nodes) {
            if (target.pin || n.parents.empty() || target.commit != n.parents.front()) continue;
            for (const auto& ref : n.refs) target.refs.push_back(ref);
        }
    }
    nodes.erase(std::remove_if(nodes.begin(), nodes.end(),
                               [](const PlmVersionNodeRow& n) { return n.pin; }),
                nodes.end());
}

bool versionRpcAbsent(const core::RpcResult& r) {
    if (!r.ok)
        return r.err.find("unknown") != std::string::npos ||
               r.err.find("not found") != std::string::npos ||
               r.err.find("method") != std::string::npos;
    if (!r.result.is_object()) return false;
    const std::string code = r.result.value("code", std::string());
    const std::string reason = r.result.value("reason", std::string());
    return code == "unknown_method" || code == "method_not_found" ||
           code == "not_implemented" || reason == "unknown_method" ||
           reason == "method_not_found" || reason == "not_implemented";
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

// ---- Team & Sync parsers (plm_status remotes/locks — contract §6, T-R7) -------

PlmRemoteRow parseRemote(const nlohmann::json& x) {
    PlmRemoteRow r;
    r.id       = getStr(x, "id");
    r.kind     = getStr(x, "kind");
    r.url      = getStr(x, "url");
    r.name     = getStr(x, "name");
    r.reserved = x.value("reserved", false) || r.kind == "chudware-cloud";
    if (auto id = x.find("identity"); id != x.end() && id->is_object()) {
        r.identityUserId    = getStr(*id, "user_id");
        r.identityDisplay   = getStr(*id, "display");
        r.identityAccountId = getStr(*id, "account_id");
    }
    if (auto c = x.find("capabilities"); c != x.end() && c->is_array()) {
        for (const auto& cap : *c) {
            if (!cap.is_string()) continue;
            const std::string s = cap.get<std::string>();
            if (s == "snapshot_sync")  r.canSync = true;
            if (s == "advisory_locks") r.canLock = true;
        }
    } else {
        // Profile predating capabilities: sync always existed; never advertise
        // locks the service has not confirmed.
        r.canSync = !r.reserved;
    }
    return r;
}

PlmLockRow parseLock(const nlohmann::json& x) {
    PlmLockRow l;
    l.itemId     = getStr(x, "item_id");
    l.userId     = getStr(x, "user_id");
    l.display    = getStr(x, "display");
    l.accountId  = getStr(x, "account_id");
    l.acquiredAt = getStr(x, "acquired_at");
    l.expiresAt  = getStr(x, "expires_at");
    return l;
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

std::string plmNiceTime(const std::string& iso) {
    // Shorten an ISO timestamp (2026-07-07T19:54:28Z) to "2026-07-07  19:54".
    if (iso.size() < 16 || iso[10] != 'T') return iso;
    return iso.substr(0, 10) + "  " + iso.substr(11, 5);
}

long long plmIsoToUnix(const std::string& iso) {
    int y = 0, mo = 0, d = 0, h = 0, mi = 0, s = 0;
    if (std::sscanf(iso.c_str(), "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &s) != 6)
        return 0;
    // Days-from-civil (Howard Hinnant) — UTC math without the timegm portability trap.
    y -= mo <= 2;
    const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = (unsigned)(y - era * 400);
    const unsigned doy = (153u * (unsigned)(mo + (mo > 2 ? -3 : 9)) + 2) / 5 + (unsigned)d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return ((long long)era * 146097 + (long long)doe - 719468) * 86400LL +
           h * 3600LL + mi * 60LL + s;
}

bool plmLockLive(const PlmLockRow& l) {
    if (l.expiresAt.empty()) return true;       // no expiry recorded → treat as live
    const long long exp = plmIsoToUnix(l.expiresAt);
    if (exp == 0) return true;                  // unparsable → warn rather than hide
    return exp > (long long)std::time(nullptr);
}

const PlmRemoteRow* plmTeamLockRemote() {
    for (const auto& rm : g_plmHub.remotes)
        if (rm.canLock && !rm.reserved) return &rm;
    return nullptr;
}

std::string plmTeamFriendlyError(const std::string& reason, const std::string& hint) {
    if (reason == "reserved_remote")
        return "Chudware Cloud is not yet available.";
    if (reason == "remote_unreachable")
        return "The shared location could not be reached. Check the folder or "
               "address and your connection, then try again.";
    if (reason == "remote_auth")
        return "The shared location refused access. Sign in or check your "
               "permissions, then try again.";
    if (reason == "contended")
        return "The shared project kept changing — try again in a moment.";
    if (reason == "locked")
        return hint.empty() ? "Someone else is editing this design." : hint;
    if (reason == "not_found")
        return "That shared location is not set up in this project.";
    if (reason == "no_vault")
        return "Project history is not available on this machine.";
    // Unknown reason: pass the hint through only when it reads like plain
    // language — transport internals never reach the user (contract §8.1).
    static const char* kLeaky[] = {"git", "Git", "GIT", "dulwich", "stderr",
                                   "refs/", "fatal:", "HEAD", "repo"};
    for (const char* t : kLeaky)
        if (hint.find(t) != std::string::npos)
            return "Something went wrong talking to the shared location.";
    return hint.empty() ? "Something went wrong talking to the shared location."
                        : hint;
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
    if (!H.histItemFilter.empty()) p["item_id"] = H.histItemFilter;
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

void plmEnsureWhereUsed(AppState& st) {
    auto& H = g_plmHub;
    if (H.wuTarget.empty()) return;
    if (H.wuLoading || (H.wuGen == H.generation && H.wuFor == H.wuTarget)) return;
    H.wuLoading = true;
    const int gen = H.generation;
    const std::string target = H.wuTarget;
    nlohmann::json p = pathParam(st);
    p["item_id"] = target;   // direct consumers only — never inferred relationships
    st.rpc.call("plm_where_used", p, [gen, target](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.wuLoading = false;
        H.wuGen = gen;
        H.wuFor = target;
        H.wuRows.clear();
        if (!plmOk(r, H.wuErr)) return;
        H.wuErr.clear();
        if (auto c = r.result.find("consumers"); c != r.result.end() && c->is_array())
            for (const auto& row : *c) {
                if (!row.is_object()) continue;
                PlmWhereUsedRow w;
                w.itemId     = getStr(row, "item_id");
                w.kind       = getStr(row, "kind");
                w.path       = getStr(row, "path");
                w.partNumber = getStr(row, "part_number");
                w.via        = getStr(row, "via");
                w.count      = row.value("count", 1);
                H.wuRows.push_back(std::move(w));
            }
    });
}

void plmEnsureVersionGraph(AppState& st) {
    auto& V = g_plmHub.versionsGraph;
    if (!V.available || V.loading || V.gen == g_plmHub.generation) return;
    V.loading = true;
    const int gen = g_plmHub.generation;
    nlohmann::json p = pathParam(st);
    p["limit"] = 400;
    p["scope"] = (V.scope == 0) ? "design" : (V.scope == 1) ? "planning" : "all";
    st.rpc.call("plm_version_graph", p, [gen](const core::RpcResult& r) {
        auto& V = g_plmHub.versionsGraph;
        V.loading = false;
        V.gen = gen;
        if (versionRpcAbsent(r)) {
            V.available = false;
            V.err = "Versions are not supported by this service.";
            return;
        }
        if (!plmOk(r, V.err)) return;
        V.err.clear();
        V.head = getStr(r.result, "head");
        V.activeBranch = getStr(r.result, "active_branch");
        V.truncated = r.result.value("truncated", false);
        V.nodes.clear();
        V.branches.clear();
        V.versions.clear();
        if (auto nodes = r.result.find("nodes"); nodes != r.result.end() && nodes->is_array())
            for (const auto& node : *nodes) V.nodes.push_back(parseVersionNode(node));
        collapseReleasePins(V.nodes);
        if (auto branches = r.result.find("branches");
            branches != r.result.end() && branches->is_array())
            for (const auto& b : *branches) {
                if (!b.is_object()) continue;
                PlmVersionBranchRow row;
                row.name = getStr(b, "name");
                row.head = getStr(b, "head");
                row.current = b.value("current", false);
                row.createdAt = getStr(b, "created_at");
                row.from = getStr(b, "from");
                row.ahead = b.value("ahead", 0);
                row.behind = b.value("behind", 0);
                row.mergeBase = getStr(b, "merge_base");
                V.branches.push_back(std::move(row));
            }
        if (auto versions = r.result.find("versions");
            versions != r.result.end() && versions->is_array())
            for (const auto& v : *versions) {
                if (!v.is_object()) continue;
                PlmNamedVersionRow row;
                row.name = getStr(v, "name");
                row.commit = getStr(v, "commit");
                row.createdAt = getStr(v, "created_at");
                row.author = getStr(v, "author");
                row.message = getStr(v, "message");
                V.versions.push_back(std::move(row));
            }
        if (V.selected.empty() && !V.nodes.empty()) V.selected = V.nodes.front().commit;
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

void plmEnsureTeam(AppState& st) {
    auto& H = g_plmHub;
    if (H.teamLoading) return;
    // Locks change on OTHER machines without any local mutation, so besides the
    // generation the cache also ages out on a slow poll while a consumer (Team
    // page, quickbar presence chip) keeps asking every frame.
    const bool stale = (H.teamGen != H.generation) ||
                       (ImGui::GetTime() - H.teamFetchedAt > 60.0);
    if (!stale) return;
    H.teamLoading = true;
    const int gen = H.generation;
    st.rpc.call("plm_status", pathParam(st), [gen](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.teamLoading = false;
        H.teamGen = gen;
        H.teamFetchedAt = ImGui::GetTime();
        std::string hint;
        if (!plmOk(r, hint)) {
            H.teamErr = plmTeamFriendlyError(plmReason(r), hint);
            return;
        }
        H.teamErr.clear();
        H.remotes.clear();
        H.locks.clear();
        if (auto rs = r.result.find("remotes"); rs != r.result.end() && rs->is_array())
            for (const auto& x : *rs)
                if (x.is_object()) H.remotes.push_back(parseRemote(x));
        if (auto ls = r.result.find("locks"); ls != r.result.end() && ls->is_array())
            for (const auto& x : *ls)
                if (x.is_object()) H.locks.push_back(parseLock(x));
    });
}

}  // namespace ui
