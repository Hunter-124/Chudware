#include "engine_events.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <mutex>
#include <map>
#include <set>
#include <string>

#include <nlohmann/json.hpp>

#include "app_state.h"
#include "engine_backend.h"      // services/worker (include dir)
#include "ipc_client.h"          // services/ipc (include dir)
#include "../renderer.h"
#include "../render/aicm_loader.h"
#include "../ui/property_manager.h"
#include "../ui/sketch_mode.h"

namespace core {

using nlohmann::json;

namespace {

// The user is mid-edit on the ACTIVE doc (open sketch or PM form) — the same
// safety gate pollAiCadHandoff used: never yank geometry out from under them.
bool userMidEdit(AppState& st) {
    return (st.sketchMode && st.sketchMode->active()) ||
           (st.propertyManager && st.propertyManager->active());
}

int tabIndexForDoc(AppState& st, const std::string& docId) {
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
        if (st.docTabs[i].docId == docId) return i;
    return -1;
}

bool pushedSnapshot(const std::string& docId, const json& data,
                    DocMirrorSnapshot& snapshot) {
    if (!data.contains("tree") || !data["tree"].is_object()) return false;
    DocMirror pushed;
    std::string ignoredPath;
    std::string err;
    if (!pushed.adoptActivateResponse(docId, data, ignoredPath, err)) return false;
    snapshot = pushed.snapshot();
    return snapshot.valid() && snapshot.docId == docId;
}

// One-time daemon bootstrap: subscribe to every doc's events + mirror docs that
// were already open in the shared engine before this GUI attached (list_docs at
// attach, per the plan). Retried while the engine reports "loading" — this runs
// on the GUI thread, so it must never block on the load.
bool bootstrapped = false;
// A failed/deferred bootstrap (engine still "loading") used to retry EVERY frame — two sync
// RPCs per frame on the GL thread. Back off: retry at most every 500ms until it succeeds.
std::chrono::steady_clock::time_point lastBootstrapTry{};
// Last reconnect generation we bootstrapped against. When EngineClient silently
// reconnects (daemon drain-replace, transient EOF), it lands on a fresh daemon
// clientId with NO event subscriptions — the one-time `bootstrapped` latch would
// otherwise leave the GUI permanently deaf to doc events (frozen live previews).
// A generation bump re-arms bootstrap so we re-subscribe + re-mirror open docs.
uint64_t bootstrapGen = 0;

// Reconnect-brake (crash-loop guard): ring of recent reconnect timestamps, pruned to the
// trailing 60s window on every reconnect event. >=3 reconnects in that window means the
// engine is crash-looping (almost always the SAME doc's cold-cache doc_activate crashing
// hostd on every re-bootstrap) — engage the brake so openMirrorTab's auto-focus stops
// re-issuing doc_activate on the newly-revealed tab (a manual tab click still activates
// normally; switchToTab never consults this). Disengages after 120s with no further
// reconnect (checked once per tick in engineEventsTick).
constexpr int kCrashLoopReconnectThreshold = 3;
constexpr std::chrono::seconds kCrashLoopWindow{60};
constexpr std::chrono::seconds kCrashLoopStableReset{120};
std::deque<std::chrono::steady_clock::time_point> reconnectTimestamps;
bool crashLoopBraked = false;
std::chrono::steady_clock::time_point lastReconnectAt{};

// Record one reconnect event, prune the window, and (re-)evaluate the brake. Returns the
// brake's state AFTER this event (true = hold automatic activation).
bool noteReconnectAndCheckBrake() {
    const auto now = std::chrono::steady_clock::now();
    lastReconnectAt = now;
    reconnectTimestamps.push_back(now);
    while (!reconnectTimestamps.empty() &&
           now - reconnectTimestamps.front() > kCrashLoopWindow)
        reconnectTimestamps.pop_front();
    if (!crashLoopBraked && (int)reconnectTimestamps.size() >= kCrashLoopReconnectThreshold) {
        crashLoopBraked = true;
        std::fprintf(stderr,
            "[engine_events] crash-loop brake engaged: %d reconnects within %llds — "
            "holding automatic doc_activate on newly revealed tabs (manual switch still works)\n",
            (int)reconnectTimestamps.size(), (long long)kCrashLoopWindow.count());
        std::fflush(stderr);
    }
    return crashLoopBraked;
}

// doc_ids with a doc.opened seen but not yet revealed as a tab (deferred while
// the user is mid-edit, exactly like the poller's `pending`), and the latest
// unapplied doc.changed payload per doc.
std::set<std::string> pendingOpen;
std::map<std::string, json> pendingChanged;

// Open a read-only mirror tab for a doc another client minted. Mirrors the
// poller's focus rules: the FIRST mirror ever opened may take focus; later
// parts open in the background with the aiPulse glow.
void openMirrorTab(AppState& st, const std::string& docId) {
    if (docId.rfind("doc-cpp", 0) != 0) return;  // parts/assemblies only (no dwg/ecad mirror yet)
    if (tabIndexForDoc(st, docId) >= 0) {        // already visible (ours or a prior mirror)
        pendingOpen.erase(docId);  // a deferred reveal is satisfied — don't retry every frame
        return;
    }
    if (userMidEdit(st)) {                       // defer the reveal, never disrupt
        pendingOpen.insert(docId);
        return;
    }
    auto* c = engine::client();
    if (!c) return;
    json r;
    std::string e;
    // Viewer semantics: attach read-only. The user's first edit attempt on this
    // tab auto-upgrades to rw (engine_backend.cpp read_only retry) — no new UI.
    c->call("attach_doc", json{{"doc_id", docId}, {"mode", "ro"}}, r, e);

    bool anyMirror = false;
    for (const auto& t : st.docTabs)
        if (t.aiHandoff) anyMirror = true;
    const bool takeFocus = !anyMirror;

    // Display name from the doc tree (doc.opened doesn't carry one). ASYNC via st.rpc:
    // doc_tree takes the doc's engine lock, and with an agent mid-op on that doc a sync
    // call here parked the whole GL thread (multiplied by every doc at bootstrap when a
    // swarm has dozens open). The tab appears immediately with a fallback title; the
    // callback (a later frame's drain) fills the real name in. Tolerate failure.
    st.rpc.call("doc_tree", json{{"doc_id", docId}},
                [&st, docId](const core::RpcResult& r) {
                    if (!r.ok || !r.result.is_object()) return;
                    std::string name;
                    if (r.result.contains("tree") && r.result["tree"].is_object())
                        name = r.result["tree"].value("name", std::string());
                    if (name.empty()) name = r.result.value("name", std::string());
                    if (name.empty()) return;
                    const int i = tabIndexForDoc(st, docId);
                    if (i < 0) return;
                    AppState::DocTab& t = st.docTabs[i];
                    if (t.title.empty() || t.title == "AI part") t.title = name;
                    if (t.handoffName.empty()) t.handoffName = name;
                }, 30000);

    AppState::DocTab t;
    t.kind = AppState::DocTab::TabKind::Part;
    t.docId = docId;
    t.title = "AI part";   // async doc_tree above fills the real name in
    t.aiHandoff = true;
    t.handoffDocId = docId;
    st.docTabs.push_back(std::move(t));
    const int idx = (int)st.docTabs.size() - 1;
    st.addLog("[daemon] mirror tab opened for " + docId);
    if (takeFocus && !crashLoopBraked) {
        st.switchToTab(idx);  // activates: doc_activate (ro-allowed) -> tree + mesh
        st.toast(ui::ToastLevel::Info,
                 "Live view of the AI's part — it updates as it builds");
    } else {
        st.docTabs[idx].aiPulse = true;  // background reveal; switch-in activates
        if (takeFocus) {
            // Crash-loop brake: would have taken focus and auto-activated, but repeated
            // engine reconnects mean this doc's activation keeps crashing hostd. Leave the
            // tab open and unfocused — the status-bar unreachable chip stays up (see
            // engineEventsTick) as the recovery affordance; a manual click still activates.
            st.docTabs[idx].activationHeldByBrake = true;
        }
    }
    pendingOpen.erase(docId);
}

// Apply a pushed foreign revision to the active live view without a blocking worker round-trip.
// The event carries the immutable tree/owner map and AICM path; disk parse runs on the mesh loader.
void applyChangedActive(AppState& st, const std::string& docId, const json& d) {
    const int idx = tabIndexForDoc(st, docId);
    if (idx != st.activeTab) return;
    AppState::DocTab& t = st.docTabs[idx];
    if (st.renderer) st.renderer->discardCachedMeshes(docId);
    t.meshCacheInvalidated = true;

    DocMirrorSnapshot snapshot;
    const bool havePushedSnapshot = pushedSnapshot(docId, d, snapshot);
    const bool mirror = t.aiHandoff;
    if (mirror) st.suppressTabDirty = true;
    if (havePushedSnapshot) {
        st.docMirror.restoreSnapshot(snapshot);
    } else {
        // Compatibility with an older daemon that emitted only revision+aicm_path.
        st.docMirror.resync(st.worker);
        snapshot = st.docMirror.snapshot();
    }
    if (mirror) st.suppressTabDirty = false;

    const std::string aicm = d.value("aicm_path", std::string());
    if (t.session) {
        t.session->cachedAicmPath = aicm;
        t.session->cachedRevision = snapshot.revision;
        t.session->cachedMirror = std::move(snapshot);
        t.session->cachedDocMesh.reset();
    }
    if (!aicm.empty()) {
        st.loadDocMeshAsync(aicm);
    } else {
        st.unloadDocMesh();
        if (st.renderer) st.renderer->setActiveMeshIdentity(docId, st.docMirror.revision());
        if (t.session) t.session->cachedDocMesh = std::make_shared<render::DocMesh>();
        t.meshCacheInvalidated = false;
    }
    pendingChanged.erase(docId);
}

// An expired edit-hold while OUR PropertyManager previews that doc: cancel the
// preview — but only undo if the doc revision is still the one the PM's
// checkpoint lineage expects (mirror in step with the engine). If a foreign
// edit already landed, undoing would destroy it: skip the undo, close the form,
// and resync instead (plan: "verify revision parentage before undo").
void handleOwnHoldExpired(AppState& st, const std::string& docId) {
    if (!st.propertyManager || !st.propertyManager->active()) return;
    if (st.docMirror.docId() != docId) return;
    auto* c = engine::client();
    long engRev = -1;
    if (c) {
        json tr;
        std::string te;
        if (c->call("doc_tree", json{{"doc_id", docId}}, tr, te) && tr.is_object() &&
            tr.contains("revision") && tr["revision"].is_number())
            engRev = tr["revision"].get<long>();
    }
    if (engRev >= 0 && engRev == (long)st.docMirror.revision()) {
        st.propertyManager->cancelEdits(st);
        st.toast(ui::ToastLevel::Warn, "Edit hold expired — preview cancelled");
    } else {
        st.propertyManager->closeIfActive();
        std::string aicm, err;
        if (st.docMirror.activate(st.worker, docId, aicm, err) && !aicm.empty()) {
            std::string le;
            st.loadDocMesh(aicm, le);
        }
        st.toast(ui::ToastLevel::Warn,
                 "Edit hold expired and the document moved — preview dropped, view resynced");
    }
}

// Daemon mode multiplexes EVERY run this client owns onto ONE `agent.event` push channel
// (hostd's sendAgentEvent is per-client, not per-run), so a streamed event is meaningless
// without its run_id. Resolve the JobState that owns it:
//   1. exact match against a run that already knows its run_id;
//   2. else bind the SOLE freshly-launched run still awaiting its first event, stamping the
//      id onto it so every later event takes path 1;
//   3. else nullptr — zero candidates, or ambiguity we must not guess through: dropping the
//      event is strictly better than cross-contaminating a concurrent conversation.
JobState* runForEvent(AppState& st, const std::string& runId) {
    if (runId.empty()) return nullptr;
    JobState* unbound = nullptr;
    int unboundCount = 0;
    for (auto& kv : st.runs) {
        JobState* run = kv.second.get();
        if (!run) continue;
        std::lock_guard<std::mutex> jlk(run->mtx);
        if (run->runId == runId) return run;
        if (run->running.load() && run->runId.empty()) { unbound = run; ++unboundCount; }
    }
    if (unboundCount != 1) return nullptr;  // nobody to bind, or ambiguous — drop
    std::lock_guard<std::mutex> jlk(unbound->mtx);
    // The launcher may have stamped a runId between our scan and this lock; if it did and it
    // isn't ours, this event belongs to a run we haven't seen yet.
    if (!unbound->runId.empty()) return unbound->runId == runId ? unbound : nullptr;
    unbound->runId = runId;
    return unbound;
}

void handleEvent(AppState& st, const json& evt) {
    const std::string name = evt.value("event", std::string());
    const json& d = evt.contains("data") && evt["data"].is_object() ? evt["data"]
                                                                    : json::object();
    const std::string docId = d.value("doc_id", std::string());

    // Live agent-run streaming (daemon mode): hostd pushes each emitted run event as
    // {"event":"agent.event","data":<ev>}. Route it into the OWNING run's event queue (matched
    // by run_id — every event carries one, including run_started) with the same "orch_" prefix
    // launchAgentRunV2's in-proc onEvent uses, so the app.cpp drain renders that conversation's
    // timeline LIVE (classifier decision, stages, assistant_delta, model I/O) instead of a
    // frozen "Classifying request…" box until the batched result.
    if (name == "agent.event") {
        JobState* job = runForEvent(st, d.value("run_id", std::string()));
        if (!job) return;  // unknown or ambiguous run — never guess a target conversation
        AgentEvent e;
        const std::string origKind = d.value("kind", std::string());
        e.kind = "orch_" + origKind;
        e.text = d.dump();
        const std::string stage = d.value("stage", std::string());
        const std::string role = d.value("role", std::string());
        if (origKind == "stage_started") {
            e.notes = stage + " > " + role + " attempt " +
                      std::to_string(d.value("attempt", 0) + 1) + "...";
        } else if (origKind == "run_finished" || origKind == "mission_finished") {
            e.notes = "run " + d.value("status", std::string());
            const std::string sr = d.value("stop_reason", std::string());
            std::string ed = d.value("error_detail", std::string());
            if (ed.empty()) ed = d.value("error", std::string());
            if (ed.empty()) ed = d.value("error_human", std::string());
            if (!sr.empty() || !ed.empty()) {
                std::lock_guard<std::mutex> jlk(job->mtx);
                if (!sr.empty() && job->stopReason.empty()) job->stopReason = sr;
                if (!ed.empty() && job->errorDetail.empty()) job->errorDetail = ed;
            }
        } else if (origKind == "provider_call") {
            const std::string prov = d.value("provider", std::string());
            const std::string model = d.value("model", std::string());
            const int http = d.value("http_status", 0);
            const int lat = d.value("latency_ms", 0);
            e.notes = prov + " \xc2\xb7 " + model +
                      (http > 0 ? " HTTP " + std::to_string(http) : std::string(" transport")) +
                      " " + std::to_string(lat) + "ms";
        } else if (origKind == "mission_task_started") {
            e.notes = d.value("title", std::string());
        } else if (origKind == "swarm_wave") {
            const int wave = d.value("wave", 0);
            const int total = d.value("total_waves", 0);
            char buf[64];
            if (total > 0)
                std::snprintf(buf, sizeof(buf), "Wave %d/%d", wave, total);
            else
                std::snprintf(buf, sizeof(buf), "Wave %d", wave);
            e.notes = buf;
        } else if (origKind == "firm_bids") {
            e.notes = "Collecting firm bids...";
        } else if (origKind == "firm_award") {
            const std::string firm = d.value("firm", std::string());
            e.notes = firm.empty() ? "Firm awarded" : ("Awarded " + firm);
        } else if (origKind == "firm_team") {
            e.notes = "Assembling firm team...";
        } else if (origKind == "firm_comm") {
            // Frequent — keep notes empty so liveStatus isn't spammed; card appends lines.
        } else if (origKind == "advisor") {
            const std::string stg = d.value("stage", std::string());
            e.notes = stg.empty() ? "Advisor" : ("Advisor \xc2\xb7 " + stg);
        } else if (origKind == "sub_run") {
            const std::string purpose = d.value("purpose", std::string());
            const std::string stt = d.value("status", std::string());
            if (!purpose.empty() && !stt.empty()) e.notes = purpose + " (" + stt + ")";
            else if (!purpose.empty()) e.notes = purpose;
            else e.notes = "Sub-run";
        }
        {
            std::lock_guard<std::mutex> lk(job->evMtx);
            job->events.push_back(std::move(e));
        }
        return;
    }

    if (name == "doc.opened") {
        if (!docId.empty()) openMirrorTab(st, docId);
        return;
    }
    if (name == "doc.changed") {
        if (docId.empty()) return;
        int idx = tabIndexForDoc(st, docId);
        if (idx < 0) {  // changed before we ever saw it (subscribe race): reveal it
            openMirrorTab(st, docId);
            idx = tabIndexForDoc(st, docId);
        }
        if (idx < 0) {  // reveal deferred (mid-edit) — remember the change
            pendingChanged[docId] = d;
            return;
        }
        // Multi-user authorship: toast when another seat edited this doc.
        // Local single-seat GUIs leave user empty — no toast.
        if (d.contains("user") && d["user"].is_object()) {
            const std::string display = d["user"].value("display", std::string());
            const std::string uid = d["user"].value("id", std::string());
            if (!display.empty()) {
                // This client's own identity (if any) is not stored on AppState yet —
                // toast whenever a non-empty foreign display is present and the source
                // client is not us (source_kind gui with empty local identity still
                // surfaces other seats; skip when display matches $USER as a soft filter).
                const char* self = std::getenv("USER");
                const bool own = self && *self && display == self;
                if (!own) {
                    st.toast(ui::ToastLevel::Info, "edited by " + display);
                    st.addLog("[daemon] doc.changed by " + display +
                              (uid.empty() ? std::string() : (" (" + uid + ")")) +
                              " on " + docId);
                }
            }
            // A remote edit reintroduces history (their undo stack); un-grey local buttons.
            st.docTabs[idx].historyCleared = false;
        }
        if (idx != st.activeTab) {
            AppState::DocTab& tab = st.docTabs[idx];
            tab.aiPulse = true;  // background activity remains visible in the tab strip
            tab.meshCacheInvalidated = true;
            if (st.renderer) st.renderer->discardCachedMeshes(docId);
            DocMirrorSnapshot snapshot;
            const std::string aicm = d.value("aicm_path", std::string());
            if (tab.session && pushedSnapshot(docId, d, snapshot) && !aicm.empty()) {
                tab.session->cachedAicmPath = aicm;
                tab.session->cachedRevision = snapshot.revision;
                tab.session->cachedMirror = std::move(snapshot);
                tab.session->cachedDocMesh.reset();
                st.cacheBackgroundDocMeshAsync(docId, tab.session->cachedRevision, aicm);
            } else if (tab.session) {
                tab.session->cachedAicmPath.clear();
                tab.session->cachedRevision = -1;
                tab.session->cachedMirror = {};
                tab.session->cachedDocMesh.reset();
            }
            return;
        }
        // Adopted mirrors stay live: the user's edits are already committed to the shared
        // engine (dirty = unsaved-to-disk, not un-pushed), so resyncing pulls the unified
        // tree containing BOTH edit streams — nothing local to clobber. Only an open
        // sketch/PM preview (userMidEdit) or a dirty tab the user owns outright defers.
        if (userMidEdit(st) || (st.docTabs[idx].dirty && !st.docTabs[idx].userAdopted)) {
            pendingChanged[docId] = d;  // defer, retry when safe (poller semantics)
            st.docTabs[idx].aiPulse = true;
            st.docTabs[idx].meshCacheInvalidated = true;
            if (st.renderer) st.renderer->discardCachedMeshes(docId);
            return;
        }
        applyChangedActive(st, docId, d);
        return;
    }
    if (name == "doc.closed") {
        const std::string reason = d.value("reason", std::string());
        const int idx = tabIndexForDoc(st, docId);
        pendingOpen.erase(docId);
        pendingChanged.erase(docId);
        if (idx < 0) return;
        if (!st.docTabs[idx].aiHandoff || st.docTabs[idx].userAdopted) {
            // A tab the user opened themselves, or an AI mirror the user ADOPTED by editing:
            // it's user-owned now. Never auto-close it — the user decides its fate (and the
            // quit warning covers it). last_writer_detached just means the agent stepped away.
            st.docTabs[idx].aiPulse = false;
            st.docTabs[idx].agentDone = true;
            st.addLog("[daemon] doc.closed for user-owned tab " + docId + " (reason " + reason + ")");
            return;
        }
        if (reason == "last_writer_detached") {
            // The agent finished this un-adopted mirror. If the user is actively looking at it
            // (active tab), keep it on screen — it auto-closes when they switch away
            // (switchToTab). Otherwise close the live view now; detach-only, so if the doc was
            // dirty the daemon's GC/retire tier preserves it for recovery (never a silent purge).
            st.docTabs[idx].aiPulse = false;
            st.docTabs[idx].agentDone = true;
            if (idx == st.activeTab) return;  // keep while viewed
            st.toast(ui::ToastLevel::Info,
                     "AI finished '" + st.docTabs[idx].displayTitle() + "' — closed (recoverable if unsaved)");
            st.closeDocTab(idx, /*detachOnly=*/true);
            return;
        }
        st.closeDocTab(idx);  // closed/gc/discarded/daemon — the doc is gone; drop the mirror
        return;
    }
    if (name == "doc.history_cleared") {
        // Human save wiped undo/redo for this doc — grey the toolbar until a new edit.
        if (docId.empty()) return;
        const int idx = tabIndexForDoc(st, docId);
        if (idx >= 0) {
            st.docTabs[idx].historyCleared = true;
            st.addLog("[daemon] history cleared for " + docId);
        }
        return;
    }
    if (name == "doc.lease_change") {
        const std::string state = d.value("state", std::string());
        const std::string holder = d.value("holder", std::string());
        if (state == "expired" && holder == "gui") {
            handleOwnHoldExpired(st, docId);
            return;
        }
        if (state == "held")
            st.toast(ui::ToastLevel::Info, "An " + holder + " client is editing " +
                                               docId + " — edits are briefly locked");
        else if (state == "disconnected")
            st.toast(ui::ToastLevel::Info, holder + " client disconnected — " + docId +
                                               " edit lock released");
        return;
    }
    // daemon.draining etc.: log only.
    if (!name.empty()) st.addLog("[daemon] event: " + name);
}

// Retired docs from a previous session awaiting explicit recovery (the daemon's
// orphan-recovery protocol): count them and raise the AppState prompt flag — the
// modal itself renders in app.cpp alongside the other app-scope confirmations.
// Best-effort: an old daemon without recovery_list just fails the call quietly.
void checkOrphanRecovery(AppState& st, chudware_ipc::EngineClient& c) {
    json rl;
    std::string e;
    if (!c.call("recovery_list", json::object(), rl, e)) return;
    if (!rl.is_object() || !rl.contains("entries") || !rl["entries"].is_array()) return;
    const int n = (int)rl["entries"].size();
    if (n <= 0) return;
    st.addLog("[daemon] " + std::to_string(n) +
              " retired doc(s) from a previous session awaiting recovery");
    st.recoveryPromptCount = n;
}

bool tryBootstrap(AppState& st, chudware_ipc::EngineClient& c) {
    json r;
    std::string e;
    if (!c.call("subscribe", json{{"doc_ids", "*"}}, r, e)) return false;
    // Docs already open in the shared engine (an agent working before the GUI
    // attached): mirror them. "loading" here just means no docs yet — retry.
    json dl;
    if (!c.call("doc_list", json::object(), dl, e)) {
        if (dl.is_object() && dl.value("code", std::string()) == "loading") return false;
        return true;  // subscribed; doc listing best-effort
    }
    if (dl.is_object() && dl.contains("docs") && dl["docs"].is_array()) {
        std::set<std::string> liveIds;
        for (const auto& doc : dl["docs"]) {
            const std::string id =
                doc.is_object() ? doc.value("doc_id", std::string()) : std::string();
            if (!id.empty()) {
                liveIds.insert(id);
                openMirrorTab(st, id);
                // origin_kind (daemon-annotated): a doc minted by a GUI client is user-owned,
                // so if we mirrored it after (re)connecting, mark it adopted — it must be
                // quit-warned and never auto-closed. Unknown/mcp/agent origin stays AI-owned.
                const std::string origin =
                    doc.is_object() ? doc.value("origin_kind", std::string()) : std::string();
                if (origin == "gui") {
                    const int ti = tabIndexForDoc(st, id);
                    if (ti >= 0 && st.docTabs[ti].aiHandoff) st.docTabs[ti].userAdopted = true;
                }
            }
        }
        // Breadcrumb for the "parts didn't show up" report: live docs mirror as
        // tabs immediately; anything not here but flagged by checkOrphanRecovery
        // below was already retired to the orphan tier (agent session ended >GC
        // grace ago) and needs the recovery prompt.
        st.addLog("[daemon] bootstrap: " + std::to_string(liveIds.size()) +
                  " live doc(s) mirrored");
        // Reconnect reconciliation: after a drain-replace we re-mirror the NEW
        // daemon's docs above, but mirror tabs for docs that were open in the OLD
        // daemon and are gone now would linger forever (no doc.closed arrives — a
        // different daemon). doc_list is authoritative, so drop any daemon-mirror
        // tab whose doc_id it no longer lists. Guards mirror the doc.closed
        // handler: only our own aiHandoff mirrors, never a user tab, never one
        // with unsaved edits, and never disrupt an active mid-edit. If we drop one
        // wrongly (transient list), a later doc.opened / re-bootstrap re-mirrors it.
        for (int i = (int)st.docTabs.size() - 1; i >= 0; --i) {
            const auto& t = st.docTabs[i];
            if (!t.aiHandoff || t.docId.empty() || liveIds.count(t.docId)) continue;
            if (t.dirty || (i == st.activeTab && userMidEdit(st))) continue;
            st.addLog("[daemon] dropping stale mirror tab " + t.docId +
                      " (absent from daemon after reconnect)");
            st.closeDocTab(i);
        }
    }
    checkOrphanRecovery(st, c);
    return true;
}

}  // namespace

void engineEventsTick(AppState& st) {
    if (!engine::daemonMode()) return;
#ifdef __EMSCRIPTEN__
    // Let a few ASYNCIFY UI frames settle before the first gateway connect (deep call stack).
    static int s_wasmWarmup = 0;
    if (s_wasmWarmup < 30) { ++s_wasmWarmup; return; }
#endif
    auto* c = engine::client();
    if (!c) return;

    // Crash-loop brake stability reset: 120s with no further reconnect clears the brake and
    // releases the sticky unreachable notice we were holding open for it (if still held).
    if (crashLoopBraked &&
        std::chrono::steady_clock::now() - lastReconnectAt >= kCrashLoopStableReset) {
        crashLoopBraked = false;
        reconnectTimestamps.clear();
        std::fprintf(stderr,
            "[engine_events] crash-loop brake disengaged after %llds of stability\n",
            (long long)kCrashLoopStableReset.count());
        std::fflush(stderr);
        if (st.daemonUnreachable) {
            st.daemonUnreachable = false;
            st.toast(ui::ToastLevel::Success, "Engine daemon stable — reconnected");
            st.addLog("[daemon] crash-loop brake cleared — daemon stable");
        }
    }

    // H2: proactive reconnect with backoff when the socket is dead (daemon crash,
    // hostd --stop, transient EOF). Without this the GUI only reconnects on the
    // next RPC and otherwise looks empty (no tabs, no status).
    const engine::DaemonConnState conn = engine::maintainConnection();
    if (conn == engine::DaemonConnState::Unreachable) {
        if (!st.daemonUnreachable) {
            st.daemonUnreachable = true;
            // One sticky notice only — status-bar chip is the persistent surface;
            // a single toast names the state without storming on every retry.
            st.toast(ui::ToastLevel::Warn, "Engine daemon unreachable — retrying");
            st.addLog("[daemon] unreachable — reconnecting with backoff");
        }
        // Do not bootstrap / drain while the socket is dead: subscribe would race
        // the backoff and spam reconnect attempts every 500ms.
        return;
    }
    if (st.daemonUnreachable) {
        const bool braked = noteReconnectAndCheckBrake();
        // Force bootstrap re-arm even if reconnectGeneration already advanced mid-call: the
        // unreachable window may have missed the gen edge. Re-arm regardless of the brake —
        // we still want a fresh doc list / subscriptions; the brake only holds back the
        // auto-focus doc_activate inside openMirrorTab.
        bootstrapped = false;
        lastBootstrapTry = {};
        if (!braked) {
            st.daemonUnreachable = false;
            st.toast(ui::ToastLevel::Success, "Engine daemon reconnected");
            st.addLog("[daemon] reconnected — re-bootstrapping doc list");
        } else {
            // Keep the sticky "unreachable" notice up instead of a misleading "reconnected" —
            // the socket is back, but repeated crashes mean the engine itself is still in
            // trouble. Cleared once the brake disengages after 120s of stability (above).
            st.addLog("[daemon] reconnected but crash-loop brake engaged (>=3 reconnects/60s) "
                      "— holding unreachable notice, skipping auto-activation");
        }
    }

    // A silent reconnect (new daemon clientId, subscriptions lost) re-arms the
    // one-time bootstrap so we re-subscribe and re-mirror the current doc set.
    const uint64_t gen = c->reconnectGeneration();
    if (gen != bootstrapGen) {
        bootstrapGen = gen;
        bootstrapped = false;
        lastBootstrapTry = {};  // retry immediately, don't wait out the backoff
    }

    if (!bootstrapped) {
        const auto now = std::chrono::steady_clock::now();
        if (now - lastBootstrapTry >= std::chrono::milliseconds(500)) {
            lastBootstrapTry = now;
            bootstrapped = tryBootstrap(st, *c);
            // First successful bootstrap = daemon connected, doc list mirrored, and the
            // orphan-recovery check has run. This is the safe point to let boot-time daemon
            // actions (the starter part) fire on web (see ensureStarterDoc).
            if (bootstrapped) st.daemonEverConnected = true;
        }
    }

    for (const json& evt : c->drainEvents()) handleEvent(st, evt);

    // Deferred work queued while the user was mid-edit / away: apply when safe.
    if (!pendingOpen.empty() && !userMidEdit(st)) {
        const std::set<std::string> retry = pendingOpen;
        for (const auto& id : retry) openMirrorTab(st, id);
    }
    if (!pendingChanged.empty() && !userMidEdit(st)) {
        // Only the ACTIVE tab applies immediately; background ones stay pulsed
        // (their next switch-in rebuild picks the change up for free).
        const std::string activeId =
            (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
                ? st.docTabs[st.activeTab].docId
                : std::string();
        auto it = pendingChanged.find(activeId);
        if (it != pendingChanged.end() &&
            (!st.docTabs[st.activeTab].dirty || st.docTabs[st.activeTab].userAdopted)) {
            const json d = it->second;
            applyChangedActive(st, activeId, d);
        }
    }
}

void mirrorRecoveredDocs(AppState& st, const std::vector<std::string>& docIds) {
    if (!engine::daemonMode() || !engine::client()) return;
    // openMirrorTab is the same path bootstrap's doc_list uses for live docs:
    // it dedups against already-open tabs, attaches ro, and focuses the first
    // reveal. Recovered docs are minted with a fresh doc_id, so none are open yet.
    for (const auto& id : docIds)
        if (!id.empty()) openMirrorTab(st, id);
}

}  // namespace core
