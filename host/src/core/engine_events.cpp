#include "engine_events.h"

#include <chrono>
#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <filesystem>
#include <mutex>
#include <map>
#include <set>
#include <string>

#include <nlohmann/json.hpp>

#include "app_state.h"
#include "tess_catchup.h"
#include "engine_backend.h"      // services/worker (include dir)
#include "ipc_client.h"          // services/ipc (include dir)
#include "../renderer.h"
#include "../render/aicm_loader.h"
#include "../ui/property_manager.h"
#include "../ui/reveng/reveng_internal.h"  // RevEngEvent / feedRevEngEvent / RevEngState
#include "../ui/manufacture/mfg_mirror.h"
#include "../ui/sketch_mode.h"
#include "../ui/sketch3d_mode.h"
#include "../ui/sim_manager.h"

namespace core {

using nlohmann::json;

namespace {
// Bounded synchronous calls made by the GL thread. EngineClient closes the
// connection on timeout; the next frame's maintainConnection restores it.
constexpr int kMetadataCallTimeoutMs = 5000;
constexpr int kAttachCallTimeoutMs = 10000;

// The user is mid-edit on the ACTIVE doc (open sketch or PM form) — the same
// safety gate pollAiCadHandoff used: never yank geometry out from under them.
bool userMidEdit(AppState& st) {
    return (st.sketchMode && st.sketchMode->active()) ||
           (st.sketch3dMode && st.sketch3dMode->active()) ||
           (st.propertyManager && st.propertyManager->active());
}

// Agent edit lock (AppState::docLockedByAgent): stamp a revision written by any client other
// than a GUI seat. Empty source_kind (older daemons) is not treated as an agent.
void stampForeignWrite(AppState& st, int idx, const json& d) {
    const std::string kind = d.value("source_kind", std::string());
    if (idx < 0 || kind.empty() || kind == "gui") return;
    st.docTabs[idx].lastForeignWriteAt = std::chrono::steady_clock::now();
}

int tabIndexForDoc(AppState& st, const std::string& docId) {
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
        if (st.docTabs[i].docId == docId) return i;
    return -1;
}

// Only a never-edited, never-saved empty CAD tab is disposable as a viewport
// placeholder. Dirty alone is insufficient: saving or undoing an edit clears it.
// A new part's mirror tree is initially {}, so revision zero is the authority
// for that case; an assembly already has its empty tree on creation.
bool activeOwnDocIsPristine(AppState& st) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size() ||
        userMidEdit(st)) return false;
    const AppState::DocTab& t = st.docTabs[st.activeTab];
    if (t.kind != AppState::DocTab::TabKind::Part &&
        t.kind != AppState::DocTab::TabKind::Assembly) return false;
    if (t.aiHandoff || t.userAdopted || t.dirty || !t.path.empty() ||
        t.docId.empty() || st.docMirror.docId() != t.docId ||
        st.docMirror.revision() != 0) return false;
    const json& tree = st.docMirror.tree();
    if (!tree.is_null() && !tree.is_object()) return false;
    for (const char* key : {"features", "components", "bodies"}) {
        const auto it = tree.find(key);
        if (it != tree.end() && (!it->is_array() || !it->empty())) return false;
    }
    return !st.renderer || st.renderer->bodyCount() == 0;
}

bool canFocusMirror(AppState& st, bool firstMirror) {
    if (activeOwnDocIsPristine(st)) return true;
    if (!firstMirror) return false;
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return true;
    const AppState::DocTab& t = st.docTabs[st.activeTab];
    return t.aiHandoff && !t.userAdopted;
}

std::string docLabel(AppState& st, const std::string& docId) {
    const int idx = tabIndexForDoc(st, docId);
    if (idx >= 0) {
        const std::string title = st.docTabs[idx].displayTitle();
        if (!title.empty()) return title;
    }
    return "a document";
}

// aiPulse means "an agent is ACTIVELY editing this doc right now" (not merely "touched
// since last viewed"): every set site stamps DocTab::lastAgentEditAt, and this per-frame
// tick clears the glow once the doc has seen no edit event for kAgentEditPulseTimeoutS —
// even if the user never views the tab. Pure timestamp comparison, so nothing stacks
// across frames or sleeps; a never-stamped (epoch) timestamp reads as already expired
// and clears on this frame's tick.
static constexpr double kAgentEditPulseTimeoutS = 5.0;

void agentEditPulseTimeoutTick(AppState& st) {
    const auto now = std::chrono::steady_clock::now();
    if (!st.anyRunActive()) st.runsIdleAt = now;  // agent-lock run epoch (docLockedByAgent)
    for (AppState::DocTab& t : st.docTabs) {
        if (!t.aiPulse) continue;
        if (std::chrono::duration<double>(now - t.lastAgentEditAt).count() >
            kAgentEditPulseTimeoutS)
            t.aiPulse = false;
    }
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
std::chrono::steady_clock::time_point lastMirrorRetry{};
std::chrono::steady_clock::time_point lastBootstrapFailureToast{};
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
// the user is mid-edit, exactly like the poller's `pending`), each mapped to the
// display name its event carried (empty when it carried none), and the latest
// unapplied doc.changed payload per doc.
std::map<std::string, std::string> pendingOpen;
std::map<std::string, json> pendingChanged;
// Viewport catch-up after tess-less revisions of the ACTIVE doc (see tess_catchup.h).
TessCatchup tessCatchup;

// Display name out of a doc.opened / doc.changed payload. doc.opened carries it
// flat (hostd DocRegistry::sniffName); doc.changed pushes the whole document as
// "tree". Empty for an older daemon that sends neither — the caller then falls
// back to the placeholder title plus an async doc_tree backfill.
std::string eventDocName(const json& d) {
    if (!d.is_object()) return {};
    if (d.contains("name") && d["name"].is_string()) return d["name"].get<std::string>();
    if (d.contains("tree") && d["tree"].is_object() && d["tree"].contains("name") &&
        d["tree"]["name"].is_string())
        return d["tree"]["name"].get<std::string>();
    return {};
}

// Open a read-only mirror tab for a doc another client minted. The first
// mirror may take focus when no user document is on screen; an agent-minted
// document may also replace an untouched empty CAD tab. Any user content
// keeps its viewport, and background reveals retain the aiPulse glow.
bool openMirrorTab(AppState& st, const std::string& docId, const std::string& name = {}) {
    if (docId.rfind("doc-cpp", 0) != 0) return true;  // parts/assemblies only
    if (tabIndexForDoc(st, docId) >= 0) {        // already visible (ours or a prior mirror)
        pendingOpen.erase(docId);  // a deferred reveal is satisfied — don't retry every frame
        return true;
    }
    // The user closed this mirror by hand. The agent keeps building (hostd degraded
    // that close to a detach), but re-revealing the tab on its next edit made the
    // close look broken. Stay closed until the user opens it again from Artifacts.
    if (st.dismissedMirrorDocs.count(docId)) {
        pendingOpen.erase(docId);
        return true;
    }
    if (userMidEdit(st)) {                       // defer the reveal, never disrupt
        // Keep a name we already know if this retry arrived without one.
        if (!name.empty() || !pendingOpen.count(docId)) pendingOpen[docId] = name;
        return true;
    }
    auto* c = engine::client();
    if (!c) return false;
    json r;
    std::string e;
    // Viewer semantics: attach read-only. The user's first edit attempt on this
    // tab auto-upgrades to rw (engine_backend.cpp read_only retry) — no new UI.
    if (!c->call("attach_doc", json{{"doc_id", docId}, {"mode", "ro"}}, r, e,
                 kAttachCallTimeoutMs)) {
        st.addLog("[daemon] attach_doc failed for " + docId + ": " + e);
        if (r.is_null()) {
            // Transport failure (including timeout): no view was confirmed. Retry
            // after reconnect instead of inventing a live mirror tab.
            if (pendingOpen.emplace(docId, name).second)
                st.toast(ui::ToastLevel::Warn, "Couldn't attach live document — retrying");
            return false;
        }
        st.toast(ui::ToastLevel::Warn, "Couldn't attach live document: " + e);
        pendingOpen.erase(docId);
        return true;  // RPC refusal: retrying this doc would only spam the daemon
    }

    bool anyMirror = false;
    for (const auto& t : st.docTabs)
        if (t.aiHandoff) anyMirror = true;
    const bool takeFocus = canFocusMirror(st, !anyMirror);
    const int activeBefore = st.activeTab;
    const std::string activeDocBefore =
        activeBefore >= 0 && activeBefore < (int)st.docTabs.size()
            ? st.docTabs[activeBefore].docId : std::string();

    // Fallback ONLY when the event carried no name (older daemon): read it off the
    // doc tree. ASYNC via st.rpc — doc_tree takes the doc's engine lock, and with an
    // agent mid-op on that doc a sync call here parked the whole GL thread
    // (multiplied by every doc at bootstrap when a swarm has dozens open). The tab
    // appears immediately with the placeholder title; the callback (a later frame's
    // drain) fills the real name in. Tolerate failure.
    if (name.empty())
        st.rpc.call("doc_tree", json{{"doc_id", docId}},
                    [&st, docId](const core::RpcResult& r) {
                        if (!r.ok || !r.result.is_object()) return;
                        std::string nm;
                        if (r.result.contains("tree") && r.result["tree"].is_object())
                            nm = r.result["tree"].value("name", std::string());
                        if (nm.empty()) nm = r.result.value("name", std::string());
                        if (nm.empty()) return;
                        const int i = tabIndexForDoc(st, docId);
                        if (i < 0) return;
                        AppState::DocTab& t = st.docTabs[i];
                        if (t.title.empty() || t.title == "AI part") t.title = nm;
                        if (t.handoffName.empty()) t.handoffName = nm;
                    }, 30000);

    AppState::DocTab t;
    t.kind = AppState::DocTab::TabKind::Part;
    t.docId = docId;
    // Primary path: doc.opened carries the doc's name, so the tab is titled correctly
    // the frame it appears — no dependency on the doc_tree round-trip above.
    t.title = name.empty() ? std::string("AI part") : name;
    t.handoffName = name;
    // The active GUI project may differ from the project of a background agent
    // turn. Attribute an unsaved mirror only when one running conversation is
    // unambiguous; otherwise it remains unscoped until saved to a project path.
    const Conversation* owner = nullptr;
    bool ambiguousOwner = false;
    for (const auto& conversation : st.conversations) {
        if (!st.convRunning(conversation.id)) continue;
        if (owner) { ambiguousOwner = true; break; }
        owner = &conversation;
    }
    if (owner && !ambiguousOwner) t.projectDir = owner->projectDir;
    t.aiHandoff = true;
    t.handoffDocId = docId;
    st.docTabs.push_back(std::move(t));
    const int idx = (int)st.docTabs.size() - 1;
    // Give the mirror its DocSession NOW, not on first switch-in. The background doc.changed
    // branch below parks the daemon's pushed snapshot + aicm_path on tab.session and prewarms
    // its mesh (cacheBackgroundDocMeshAsync) — every one of those writes is guarded on
    // `tab.session`, so a never-activated mirror (any part after the first: takeFocus is false
    // once one mirror exists) silently dropped every pushed revision while still setting
    // meshCacheInvalidated. Clicking such a tab then found an empty cache, wiped the scene
    // (switchToTab's unloadDocMesh) and issued a doc_activate that queues on the doc's
    // exclusive per-doc engine mutex behind the agent's write burst — the user got the
    // "Loading" scrim with no geometry and no picking for as long as the agent kept writing.
    // With the session present the switch binds from the parked snapshot instead (no RPC at
    // all once the prewarm committed), so spectating and selecting never wait on the writer.
    st.ensureTabSession(st.docTabs[idx]);
    st.addLog("[daemon] mirror tab opened for " + docId);
    // Prime the tab from the daemon's last pushed revision BEFORE anything can switch to
    // it. This is the lock-free read (primeMirrorFromSnapshot); it lands within a frame or
    // two and leaves a parked snapshot + prewarmed mesh, so the auto-focus below and any
    // later user click both take switchToTab's pushed/cached path instead of issuing a
    // doc_activate that queues on the doc's engine mutex behind the agent writing it.
    if (takeFocus && !crashLoopBraked) {
        core::primeMirrorFromSnapshot(st, docId,
                                      [&st, docId, activeBefore, activeDocBefore, anyMirror](bool) {
            // A user can switch or edit tabs while the snapshot request is in
            // flight. Never steal the viewport after that change of intent.
            if (userMidEdit(st) || crashLoopBraked || st.activeTab != activeBefore ||
                (activeBefore >= 0 &&
                 (activeBefore >= (int)st.docTabs.size() ||
                  st.docTabs[activeBefore].docId != activeDocBefore)))
                return;
            const int i = tabIndexForDoc(st, docId);
            if (i >= 0 && i != st.activeTab &&
                canFocusMirror(st, !anyMirror)) st.switchToTab(i);
        });
        st.toast(ui::ToastLevel::Info,
                 "Live view of the AI's part — it updates as it builds");
    } else {
        st.docTabs[idx].aiPulse = true;  // background reveal; switch-in activates
        st.docTabs[idx].lastAgentEditAt = std::chrono::steady_clock::now();
        core::primeMirrorFromSnapshot(st, docId);
        if (takeFocus) {
            // Crash-loop brake: would have taken focus and auto-activated, but repeated
            // engine reconnects mean this doc's activation keeps crashing hostd. Leave the
            // tab open and unfocused — the status-bar unreachable chip stays up (see
            // engineEventsTick) as the recovery affordance; a manual click still activates.
            st.docTabs[idx].activationHeldByBrake = true;
        }
    }
    pendingOpen.erase(docId);
    return true;
}

void applyChangedActive(AppState& st, const std::string& docId, const json& d);

// The active view just applied a revision that carried no tessellation. Ask the daemon for
// the current mesh (doc_activate: the warm read-cache answer, no recompile; an assembly's
// AICM is its cached component meshes re-posed) and draw it when it still matches the tree
// on screen. Async on st.rpc — never on the GL thread — and single-flight per doc, so a pose
// sweep costs at most one outstanding request.
void fetchActiveTess(AppState& st, const std::string& docId) {
    st.rpc.call("doc_activate", json{{"doc_id", docId}}, [&st, docId](const core::RpcResult& r) {
        const json& res = r.result;
        // A tree is required: without one applyChangedActive would fall back to a SYNC resync.
        const bool usable = r.ok && res.is_object() && !res.contains("code") &&
                            res.contains("revision") && res["revision"].is_number_integer() &&
                            res.contains("tree") && res["tree"].is_object() &&
                            res.contains("tess") && res["tess"].is_object() &&
                            !res["tess"].value("aicm_path", std::string()).empty();
        const long rev = usable ? res["revision"].get<long>() : -1;
        const int idx = tabIndexForDoc(st, docId);
        // A deferred push for this doc (pendingChanged) is newer than anything drawn now.
        const bool viewing = idx >= 0 && idx == st.activeTab && st.docMirror.docId() == docId &&
                             !userMidEdit(st) && !pendingChanged.count(docId);
        switch (tessCatchup.onFetched(docId, usable, rev, viewing,
                                      (long)st.docMirror.revision())) {
        case TessCatchup::Outcome::Commit: {
            // Same shape as a doc.changed push (doc_snapshot serves that shape verbatim too).
            json d = res;
            d["doc_id"] = docId;
            d["aicm_path"] = res["tess"]["aicm_path"];
            applyChangedActive(st, docId, d);
            break;
        }
        case TessCatchup::Outcome::Refetch:
            fetchActiveTess(st, docId);
            break;
        case TessCatchup::Outcome::Drop:
            if (!r.ok || (res.is_object() && res.contains("code")))
                st.addLog("[daemon] viewport mesh catch-up failed for " + docId + ": " +
                          (r.ok ? res.value("message", std::string()) : r.err));
            break;
        }
    });
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

    // A revision published without a tessellation (asm_add_component / asm_mate and the
    // animation pose writers call doc_apply_ops with with_tess:false) carries NO aicm_path
    // key; an empty document carries an EMPTY one. Only the latter means "nothing to draw".
    // For the former keep the displayed mesh (unloading blanked the viewport after every
    // agent mate), drop the tab's cached mesh so a switch-in never binds a mesh older than
    // its tree, and fetch the current tessellation: waiting for the next mesh-bearing
    // revision left an assembly's components unmoved for a whole mate/sweep stretch.
    if (!d.contains("aicm_path")) {
        if (t.session) {
            t.session->cachedAicmPath.clear();
            t.session->cachedRevision = -1;
            t.session->cachedMirror = {};
            t.session->cachedDocMesh.reset();
        }
        pendingChanged.erase(docId);
        if (tessCatchup.wantMesh(docId, (long)snapshot.revision)) fetchActiveTess(st, docId);
        return;
    }
    tessCatchup.satisfied(docId);
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
        if (!c->call("doc_tree", json{{"doc_id", docId}}, tr, te,
                     kMetadataCallTimeoutMs)) {
            st.addLog("[daemon] doc_tree revision check failed for " + docId + ": " + te);
            st.propertyManager->closeIfActive();  // cannot safely undo an unverified revision
            st.toast(ui::ToastLevel::Warn,
                     "Edit hold expired — revision unavailable; edit form closed without undo");
            return;
        }
        if (tr.is_object() && tr.contains("revision") && tr["revision"].is_number())
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
    // Manufacturing is a dedicated hostd lane, but its events arrive through
    // the same per-frame daemon drain. Feed the read-only GUI mirror and keep
    // them out of unrelated document/agent routing.
    if (name.rfind("mfg.", 0) == 0) {
        if (st.mfgMirror) st.mfgMirror->feedEvent(evt);
        return;
    }

    // Live agent-run streaming (daemon mode): hostd pushes each emitted run event as
    // {"event":"agent.event","data":<ev>}. Route it into the OWNING run's event queue (matched
    // by run_id — every event carries one, including run_started) with the same "orch_" prefix
    // launchAgentRunV2's in-proc onEvent uses, so the app.cpp drain renders that conversation's
    // timeline LIVE (thinking bursts, tool-call rows, assistant_delta, stage rows) instead of
    // a frozen "Thinking…" placeholder until the batched result.
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
            // error_human is the actionable one-liner, NOT a stand-in for the raw detail —
            // keep both so the card can headline the guidance and still show the body.
            const std::string eh = d.value("error_human", std::string());
            if (ed.empty()) ed = eh;
            if (!sr.empty() || !ed.empty() || !eh.empty()) {
                std::lock_guard<std::mutex> jlk(job->mtx);
                if (!sr.empty() && job->stopReason.empty()) job->stopReason = sr;
                if (!ed.empty() && job->errorDetail.empty()) job->errorDetail = ed;
                if (!eh.empty() && job->errorHuman.empty()) job->errorHuman = eh;
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
        if (!docId.empty()) openMirrorTab(st, docId, eventDocName(d));
        stampForeignWrite(st, tabIndexForDoc(st, docId), d);
        return;
    }
    if (name == "doc.changed") {
        if (docId.empty()) return;
        int idx = tabIndexForDoc(st, docId);
        if (idx < 0) {  // changed before we ever saw it (subscribe race): reveal it
            openMirrorTab(st, docId, eventDocName(d));
            idx = tabIndexForDoc(st, docId);
        }
        if (idx < 0) {  // reveal deferred (mid-edit) — remember the change
            pendingChanged[docId] = d;
            return;
        }
        stampForeignWrite(st, idx, d);
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
            tab.lastAgentEditAt = std::chrono::steady_clock::now();
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
        // Every tab stays live: in daemon mode the user's own edits are committed to the shared
        // engine as they happen (dirty = unsaved-to-disk, never un-pushed), so the pushed
        // snapshot already carries BOTH edit streams and there is nothing local to clobber. Only
        // an open sketch/PM preview (userMidEdit) defers. Deferring a dirty user-owned tab froze
        // the viewport for a whole agent turn: the first applied agent revision lights the tab's
        // dirty marker, and from then on every later revision was parked until the user saved
        // (2026-09-25, a 2 h steering rework on the user's open assembly showed nothing).
        if (userMidEdit(st)) {
            pendingChanged[docId] = d;  // defer, retry when safe (poller semantics)
            st.docTabs[idx].aiPulse = true;
            st.docTabs[idx].lastAgentEditAt = std::chrono::steady_clock::now();
            st.docTabs[idx].meshCacheInvalidated = true;
            if (st.renderer) st.renderer->discardCachedMeshes(docId);
            return;
        }
        applyChangedActive(st, docId, d);
        return;
    }
    if (name == "doc.studies_changed") {
        // An agent persisted a study (run_fea_* results, a simulate_motion sweep) — a revision
        // with no tree, so doc.changed never carries it. Re-pull that document's study list on
        // its next Simulation-tree draw. GUI-sourced edits already live in the local mirror.
        if (docId.empty() || d.value("source_kind", std::string()) == "gui") return;
        const int idx = tabIndexForDoc(st, docId);
        if (idx >= 0 && st.docTabs[idx].session && st.docTabs[idx].session->sim)
            st.docTabs[idx].session->sim->markStudiesStale();
        return;
    }
    if (name == "doc.closed") {
        const std::string reason = d.value("reason", std::string());
        const int idx = tabIndexForDoc(st, docId);
        pendingOpen.erase(docId);
        pendingChanged.erase(docId);
        if (idx < 0) return;
        // The writer detached: an agent that is no longer running cannot be mid-edit. A run
        // still live elsewhere keeps the lock (another sub-agent may still write this doc).
        if (!st.anyRunActive())
            st.docTabs[idx].lastForeignWriteAt = std::chrono::steady_clock::time_point{};
        if (!st.docTabs[idx].aiHandoff || st.docTabs[idx].userAdopted) {
            // A tab the user opened themselves, or an AI mirror the user ADOPTED by editing:
            // it's user-owned now. Never auto-close it — the user decides its fate (and the
            // quit warning covers it).
            st.docTabs[idx].aiPulse = false;
            st.docTabs[idx].agentDone = true;
            st.addLog("[daemon] doc.closed for user-owned tab " + docId + " (reason " + reason + ")");
            return;
        }
        if (reason == "last_writer_detached") {
            // NOT a close. hostd emits this the moment the last read-write ATTACH drops, and the
            // general lane spawns a fresh `python -m services.worker_main` per turn, so it fires at
            // the END OF EVERY TURN — the doc is still live and still returned by doc_list right
            // after. So keep the tab: it is the user's ONLY handle on the part the agent just built
            // (the viewport view, and the aiHandoff flag every agent-document surface keys off —
            // Artifacts "Agent Documents", the AI-tab Documents view, Active Work). Destroying it
            // here is what made the finished part vanish from the tab strip and those panels fall
            // back to the user's own documents. Mark the agent idle and leave the tab standing;
            // only a real close/gc/discard (below) removes it.
            st.docTabs[idx].aiPulse = false;
            st.docTabs[idx].agentDone = true;
            st.addLog("[daemon] last writer detached from AI mirror " + docId +
                      " — keeping the tab (doc is still live daemon-side)");
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
                                               docLabel(st, docId) + " — edits are briefly locked");
        else if (state == "disconnected")
            st.toast(ui::ToastLevel::Info, holder + " client disconnected — " + docLabel(st, docId) +
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
void reportBootstrapTransportFailure(AppState& st, const std::string& detail) {
    st.addLog("[daemon] " + detail);
    const auto now = std::chrono::steady_clock::now();
    if (now - lastBootstrapFailureToast >= std::chrono::seconds(15)) {
        lastBootstrapFailureToast = now;
        st.toast(ui::ToastLevel::Warn, "Engine document sync interrupted — retrying");
    }
}

bool checkOrphanRecovery(AppState& st, chudware_ipc::EngineClient& c) {
    json rl;
    std::string e;
    if (!c.call("recovery_list", json::object(), rl, e, kMetadataCallTimeoutMs)) {
        if (!c.connected()) {
            reportBootstrapTransportFailure(st, "recovery_list transport failure: " + e);
            return false;  // reconnect dropped the subscription; bootstrap anew
        }
        st.addLog("[daemon] recovery_list unavailable: " + e);
        return true;  // older daemons do not support orphan recovery
    }
    if (!rl.is_object() || !rl.contains("entries") || !rl["entries"].is_array()) return true;
    const int n = (int)rl["entries"].size();
    if (n <= 0) return true;
    st.addLog("[daemon] " + std::to_string(n) +
              " retired doc(s) from a previous session awaiting recovery");
    st.recoveryPromptCount = n;
    return true;
}

bool tryBootstrap(AppState& st, chudware_ipc::EngineClient& c) {
    json r;
    std::string e;
    if (!c.call("subscribe", json{{"doc_ids", "*"}}, r, e,
                kMetadataCallTimeoutMs)) {
        if (!c.connected()) reportBootstrapTransportFailure(st, "subscribe failed: " + e);
        else st.addLog("[daemon] subscribe failed: " + e);
        return false;
    }
    // Docs already open in the shared engine (an agent working before the GUI
    // attached): mirror them. "loading" here just means no docs yet — retry.
    json dl;
    if (!c.call("doc_list", json::object(), dl, e, kMetadataCallTimeoutMs)) {
        if (dl.is_object() && dl.value("code", std::string()) == "loading") return false;
        if (!c.connected()) {
            reportBootstrapTransportFailure(st, "doc_list failed: " + e);
            return false;  // never reconcile tabs against an unconfirmed list
        }
        st.addLog("[daemon] doc_list failed: " + e);
        return checkOrphanRecovery(st, c);  // subscribed; list unavailable, no reconciliation
    }
    if (dl.is_object() && dl.contains("docs") && dl["docs"].is_array()) {
        std::set<std::string> liveIds;
        for (const auto& doc : dl["docs"]) {
            const std::string id =
                doc.is_object() ? doc.value("doc_id", std::string()) : std::string();
            if (!id.empty()) {
                liveIds.insert(id);
                if (!openMirrorTab(st, id, eventDocName(doc))) return false;
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
    return checkOrphanRecovery(st, c);
}

// ---------------------------------------------------------------------------
// Reverse-Engineering reconstruction progress (contracts/reveng.md §4)
// ---------------------------------------------------------------------------
//
// WHY A JOURNAL TAIL AND NOT A PUSH CHANNEL OR A POLL
//
//   * The agent.event push above cannot carry these. hostd arms the streaming sink
//     (core::cpp_core().setEventSink) only for agent_* / session_btw / session_handoff, and it
//     is fed by the C++ core (core/hostd/server.cpp). A reveng mission runs inside the PYTHON
//     worker — services/mcp/reveng_common.py::_launch_reveng_mission -> services/dag/executor
//     .run() on a daemon thread — and never touches that sink, so no reveng record ever reaches
//     this client as an agent.event.
//
//   * WorkerClient::EventFn streaming cannot carry them either, at three independent breaks:
//     the transport adapter's handlers ignore their emit argument (services/reveng/
//     worker_rpc.py::_bridge), hostd's WorkerBridge takes no EventFn and DISCARDS every
//     {"id",…,"event":…} frame the worker writes (core/hostd/worker_bridge.cpp), and AsyncRpc —
//     what the reveng GUI uses for every call — drops onEvent entirely (core/async_rpc.cpp).
//
//   * Polling is now *possible* (reveng_reconstruct returns in ~200 ms and the mission runs on
//     its own thread) but it cannot draw this UI. The only pollable mirror is session.run, and
//     _run_summary() puts exactly {run_id, template, status, gate, started_at, updated_at,
//     error} on it — no stage, no pct — refreshed only at launch, at a gate pause, and at the
//     end. That is the coarse badge the Reconstruct stage already had. A progress bar needs the
//     per-stage records, and those exist only on the journal.
//
// The journal is push-quality without the push: EventJournal.emit() flushes AND fsyncs every
// record before returning (services/dag/event_journal.py), so events.jsonl is a live, ordered,
// crash-consistent feed on local disk carrying every stage the mission passes through —
// including the intra-node dense/splat/mesh records make_reveng_stage_emitter writes, which no
// RPC surface exposes at all. Tailing it is plain bounded file I/O: no worker round-trip per
// frame, no engine mutex, and it cannot park the GL thread the way a sync RPC would.
//
// ADDRESSING: direct, by the run_id reveng_reconstruct returns. _launch_reveng_mission builds
// the emitters and writes its first record ("capture", 0, "mission … queued") on the CALLING
// thread before spawning, so <cache>/runs/<run_id>/events.jsonl provably exists the instant the
// GUI holds the run_id. There is no discovery heuristic and no window to cover.

namespace fs = std::filesystem;

constexpr std::chrono::milliseconds kRevEngPollInterval{400};
// The journal is written before the run_id is handed out, so a file that never appears means
// the GUI and the worker disagree about the data dir (different XDG_DATA_HOME / APPDATA) — a
// deployment fault, not a slow mission. Stop stat-ing rather than poll a path forever.
constexpr std::chrono::seconds kRevEngJournalWaitWindow{30};
// Never read more than this per tick. A bigger backlog means only the tail still matters.
constexpr std::uintmax_t kRevEngMaxTailBytes = 1u << 20;

struct RevEngWatch {
    std::string runId;            // "" = idle; the whole feature then costs one compare/frame
    fs::path journal;             // <cache>/runs/<run_id>/events.jsonl
    bool opened = false;          // the journal has been read at least once
    std::uintmax_t consumed = 0;  // bytes of `journal` already parsed
    std::string carry;            // trailing partial line from the previous read
    std::string errMsg;           // most specific failure text seen so far (beats run status)
    // Last event we fed. The Reconstruct stage has other writers — the kick callback's initial
    // seed, and the 3 s get_reveng_session badge poll, which flips recon.done/pct=100 as soon as
    // session.stages.reconstruct reads "done" even though the mission is still running fit /
    // parametric / review. While this watch is armed the journal outranks both, so a state that
    // no longer matches what we fed is restored on the next tick.
    std::string lastKind;
    int lastPct = 0;
    std::string lastMsg;
    std::chrono::steady_clock::time_point armedAt{};
    std::chrono::steady_clock::time_point lastPoll{};
};
RevEngWatch revengWatch;

// Mirrors services/dag/event_journal.py::_default_cache_runs_dir() exactly. Empty when the
// environment names no home at all (the tail then no-ops).
fs::path revengCacheRunsDir() {
#ifdef _WIN32
    if (const char* appdata = std::getenv("APPDATA"); appdata && *appdata)
        return fs::path(appdata) / "Chudware" / "cache" / "runs";
    if (const char* home = std::getenv("USERPROFILE"); home && *home)
        return fs::path(home) / "AppData" / "Roaming" / "Chudware" / "cache" / "runs";
#else
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && *xdg)
        return fs::path(xdg) / "Chudware" / "cache" / "runs";
    if (const char* home = std::getenv("HOME"); home && *home)
        return fs::path(home) / ".local" / "share" / "Chudware" / "cache" / "runs";
#endif
    return {};
}

// The run's journal, addressed directly.
fs::path revengJournalPath(const std::string& runId) {
    const fs::path runs = revengCacheRunsDir();
    if (runs.empty() || runId.empty()) return {};
    return runs / runId / "events.jsonl";
}

void revengFeed(RevEngWatch& w, const std::string& kind, int pct, const std::string& msg) {
    ui::reveng::RevEngEvent ev;
    ev.kind = kind;
    ev.pct = pct;
    ev.msg = msg;
    ui::reveng::feedRevEngEvent(ev);
    w.lastKind = kind;   // self-assignment-safe: these may alias w's own members
    w.lastPct = pct;
    w.lastMsg = msg;
}

// Does RevEngState still show what we last fed? feedRevEngEvent's assignments are mirrored
// exactly, so a false here means another writer (the kick seed, the badge poll) overwrote the
// journal's answer.
bool revengStateMatchesLast(const RevEngWatch& w) {
    const ui::reveng::ReconstructState& r = ui::reveng::S().recon;
    if (w.lastKind == "reconstruct_progress")
        return r.active && !r.done && r.pct == w.lastPct && r.phase == w.lastMsg;
    if (w.lastKind == "reconstruct_done")
        return r.done && !r.active && r.pct == 100;
    if (w.lastKind == "reconstruct_error")
        return !r.active &&
               r.err == (w.lastMsg.empty() ? std::string("Unknown reconstruction error")
                                           : w.lastMsg);
    return true;  // nothing fed yet — the seed is the only information there is
}

// One events.jsonl record -> at most one RevEngEvent. Returns true when the record ENDS the
// run. Terminality comes from the journal alone: the launch RPC now returns in ~200 ms with
// status "running", so the RPC lifecycle says nothing about whether the mission is over.
//
//   aicad.reveng_events/1  -> "reconstruct_progress". pct verbatim (clamped); msg = the §4
//                             `stage` word (capture|scale|sparse|dense|splat|mesh|fit|
//                             parametric|review|deliver) and NOTHING ELSE. The Reconstruct
//                             sub-step track substring-matches "mesh"/"dense"/"sparse" against
//                             recon.phase in that order, so carrying the record's own prose
//                             ("dense MVS complete") could bind the wrong sub-step. The worker
//                             writes `stage` bare for the same reason; `msg` holds the prose.
//                             No session filter: the run_id IS the address, and one journal
//                             belongs to exactly one run.
//   dag.node.failed        -> "reconstruct_error" with the node's escalation text. This is the
//                             ONLY failure signal on the journal: make_reveng_emit translates
//                             dag.node.started/done/paused and nothing else. NOT terminal — a
//                             retry edge can route back and resume progress, which the next
//                             progress event clears the error for.
//   dag.run.done           -> terminal, EXCEPT status "paused". A pause is an approve gate
//                             waiting on the human; reveng_resolve_gate resumes the SAME run_id
//                             onto the SAME journal, so the tail must stay armed across it or
//                             it would go blind exactly when the run comes back to life.
bool revengApplyRecord(RevEngWatch& w, const json& r) {
    const std::string kind = r.value("kind", std::string());
    if (kind == "aicad.reveng_events/1") {
        int pct = r.value("pct", 0);
        pct = pct < 0 ? 0 : (pct > 100 ? 100 : pct);
        revengFeed(w, "reconstruct_progress", pct, r.value("stage", std::string()));
        return false;
    }
    if (kind == "dag.node.failed") {
        const std::string node = r.value("node_id", std::string());
        std::string text = r.value("error", std::string());
        if (text.empty()) text = "node failed";
        w.errMsg = node.empty() ? text : (node + ": " + text);
        revengFeed(w, "reconstruct_error", 0, w.errMsg);
        return false;
    }
    if (kind == "dag.run.done") {
        const std::string status = r.value("status", std::string());
        if (status == "paused") return false;
        if (status == "done")
            revengFeed(w, "reconstruct_done", 100, std::string());
        else
            revengFeed(w, "reconstruct_error", 0,
                       w.errMsg.empty() ? ("reconstruction run " + status) : w.errMsg);
        return true;
    }
    return false;
}

// Apply whatever the mission appended since the last tick. Bounded, non-blocking file I/O.
// Returns true when a terminal record landed.
bool revengConsume(RevEngWatch& w) {
    std::error_code ec;
    const std::uintmax_t size = fs::file_size(w.journal, ec);
    if (ec || size > (std::uintmax_t)LONG_MAX) return false;
    w.opened = true;
    if (size < w.consumed) { w.consumed = 0; w.carry.clear(); }  // rotated / truncated
    if (size == w.consumed) return false;
    std::uintmax_t from = w.consumed;
    if (size - from > kRevEngMaxTailBytes) {  // absurd backlog: only the newest state matters
        from = size - kRevEngMaxTailBytes;
        w.carry.clear();
    }
    std::FILE* f = std::fopen(w.journal.string().c_str(), "rb");
    if (!f) return false;
    if (std::fseek(f, (long)from, SEEK_SET) != 0) { std::fclose(f); return false; }
    std::string buf = std::move(w.carry);
    w.carry.clear();
    char chunk[16384];
    std::uintmax_t got = 0;
    while (got < size - from) {
        const size_t n = std::fread(chunk, 1, sizeof(chunk), f);
        if (n == 0) break;
        buf.append(chunk, n);
        got += n;
    }
    std::fclose(f);
    w.consumed = from + got;

    bool terminal = false;
    size_t start = 0;
    for (;;) {
        const size_t nl = buf.find('\n', start);
        if (nl == std::string::npos) break;
        const std::string line = buf.substr(start, nl - start);
        start = nl + 1;
        if (line.empty()) continue;
        // A half-written or foreign line is not a reason to stop draining the rest.
        try {
            const json rec = json::parse(line);
            if (rec.is_object() && revengApplyRecord(w, rec)) terminal = true;
        } catch (...) {
        }
    }
    if (start < buf.size()) w.carry = buf.substr(start);  // partial trailing line
    return terminal;
}

// Once per frame. Cheap when idle: one empty-string test.
void revengReconstructTick() {
    RevEngWatch& w = revengWatch;
    if (w.runId.empty()) return;

    // Only the file read is throttled.
    const auto now = std::chrono::steady_clock::now();
    if (now - w.lastPoll >= kRevEngPollInterval) {
        w.lastPoll = now;
        if (revengConsume(w)) {      // dag.run.done (not "paused") — the mission is over
            w = RevEngWatch{};
            return;
        }
        if (!w.opened && now - w.armedAt > kRevEngJournalWaitWindow) {
            // The journal is written before the run_id is handed out, so it should be readable
            // on the very first tick. Still absent after the window = the GUI and the worker
            // resolve different data dirs; stop rather than stat a path that never appears.
            std::fprintf(stderr,
                "[engine_events] reveng: no journal at %s — GUI and worker disagree on the "
                "Chudware data dir; Reconstruct progress will fall back to the stage badge\n",
                w.journal.string().c_str());
            std::fflush(stderr);
            w = RevEngWatch{};
            return;
        }
    }
    if (!w.opened) return;

    // While this watch is armed the journal outranks every other writer of ReconstructState —
    // the kick callback's initial seed and, more importantly, the 3 s get_reveng_session badge
    // poll, which flips recon.done/pct=100 the moment session.stages.reconstruct reads "done"
    // even though the mission still has fit / parametric / review to run. This is deliberately
    // NOT throttled with the read above: it is three comparisons, and st.rpc.drain() runs
    // earlier in the frame than engineEventsTick(), so correcting here means a foreign write is
    // gone before anything draws instead of flickering "complete / 100%" for up to a poll
    // interval. It only feeds when something actually overwrote us.
    if (!revengStateMatchesLast(w)) revengFeed(w, w.lastKind, w.lastPct, w.lastMsg);
}

std::string refusalText(const json& refusal, const char* key) {
    const auto it = refusal.is_object() ? refusal.find(key) : refusal.end();
    return it != refusal.end() && it->is_string() ? it->get<std::string>() : std::string();
}

// The chip tooltip for a daemon_draining refusal: the daemon's own message and hint.
// Empty when the daemon does not refuse this client.
std::string drainNotice(const json& refusal) {
    if (!refusal.is_object()) return std::string();
    std::string notice = refusalText(refusal, "message");
    const std::string hint = refusalText(refusal, "hint");
    if (!hint.empty()) notice += (notice.empty() ? "" : "\n") + hint;
    return notice.empty() ? std::string("The engine daemon is draining. It accepts no new "
                                        "clients until it exits.")
                          : notice;
}

}  // namespace

void watchRevEngReconstruct(const std::string& runId) {
    revengWatch = RevEngWatch{};
    if (runId.empty()) return;  // explicit stop
    revengWatch.runId = runId;
    revengWatch.journal = revengJournalPath(runId);
    revengWatch.armedAt = std::chrono::steady_clock::now();
    if (revengWatch.journal.empty()) revengWatch = RevEngWatch{};  // no resolvable data dir
    // lastPoll stays at the clock epoch so the very first tick polls without waiting.
}

void engineEventsTick(AppState& st) {
    // Pulse timeout runs in BOTH daemon and legacy mode — the legacy .step handoff poller
    // (feature_tree.cpp pollAiCadHandoff) stamps the same pulse — so it sits ahead of the
    // daemon-only gate below, at the very top of the frame.
    agentEditPulseTimeoutTick(st);
    // The reveng journal tail is a LOCAL FILE read, not a daemon push, so it runs in both
    // daemon and legacy in-process mode — ahead of the daemon-only gate below.
    revengReconstructTick();
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
        // A draining hostd refuses this client by name: the chip shows why until it exits.
        const json refusal = engine::daemonRefusal();
        const std::string drain = drainNotice(refusal);
        if (!st.daemonUnreachable) {
            st.daemonUnreachable = true;
            // One sticky notice only — status-bar chip is the persistent surface;
            // a single toast names the state without storming on every retry.
            st.toast(ui::ToastLevel::Warn,
                     drain.empty() ? "Engine daemon unreachable — retrying"
                                   : "Engine daemon is draining. Chudware connects when it exits.");
            st.addLog("[daemon] unreachable — reconnecting with backoff");
        }
        if (drain != st.daemonDrainNotice) {
            if (!drain.empty())
                st.addLog("[daemon] draining (" + refusalText(refusal, "reason") +
                          ") — refused this client: " + refusalText(refusal, "message"));
            st.daemonDrainNotice = drain;
        }
        // Do not bootstrap / drain while the socket is dead: subscribe would race
        // the backoff and spam reconnect attempts every 500ms.
        return;
    }
    st.daemonDrainNotice.clear();
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
            const uint64_t before = c->reconnectGeneration();
            const bool ok = tryBootstrap(st, *c);
            // A timed-out call invalidates its socket. Any inline reconnect
            // during bootstrap also loses the earlier subscription.
            bootstrapped = ok && c->connected() &&
                           before == c->reconnectGeneration();
            if (bootstrapped) st.daemonEverConnected = true;
        }
    }

    for (const json& evt : c->drainEvents()) handleEvent(st, evt);

    // Deferred work queued while the user was mid-edit / away: apply when safe.
    if (bootstrapped && c->connected() && !pendingOpen.empty() && !userMidEdit(st) &&
        std::chrono::steady_clock::now() - lastMirrorRetry >= std::chrono::milliseconds(500)) {
        lastMirrorRetry = std::chrono::steady_clock::now();
        const std::map<std::string, std::string> retry = pendingOpen;
        for (const auto& [id, nm] : retry)
            if (!openMirrorTab(st, id, nm) && !c->connected()) break;
    }
    if (!pendingChanged.empty() && !userMidEdit(st)) {
        // Only the ACTIVE tab applies immediately; background ones stay pulsed
        // (their next switch-in rebuild picks the change up for free).
        const std::string activeId =
            (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
                ? st.docTabs[st.activeTab].docId
                : std::string();
        auto it = pendingChanged.find(activeId);
        if (it != pendingChanged.end()) {
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
    // recovery_open hands back ids only, so these take openMirrorTab's name-less
    // path: placeholder title now, async doc_tree backfill on a later frame.
    for (const auto& id : docIds)
        if (!id.empty()) openMirrorTab(st, id);
}

void primeMirrorFromSnapshot(AppState& st, const std::string& docId,
                             std::function<void(bool)> then) {
    if (docId.empty() || !engine::daemonMode() || !engine::client()) {
        if (then) then(false);
        return;
    }
    st.rpc.call("doc_snapshot", json{{"doc_id", docId}},
                [&st, docId, then = std::move(then)](const core::RpcResult& r) {
                    const bool have = r.ok && r.result.is_object() &&
                                      r.result.value("has_snapshot", false);
                    if (!have) {
                        // An old daemon (unknown method) or a doc that has not been written
                        // since the daemon started. The caller falls back to doc_activate.
                        if (then) then(false);
                        return;
                    }
                    const int idx = tabIndexForDoc(st, docId);
                    if (idx < 0) {
                        if (then) then(false);
                        return;
                    }
                    // doc_snapshot returns the VERBATIM doc.changed payload, so both the
                    // active and the background branch below are the same code the live
                    // event path already runs — this is a replay of the push we missed,
                    // not a second way of loading a document.
                    if (idx == st.activeTab) {
                        applyChangedActive(st, docId, r.result);
                        if (then) then(true);
                        return;
                    }
                    AppState::DocTab& tab = st.docTabs[idx];
                    DocMirrorSnapshot snapshot;
                    const std::string aicm = r.result.value("aicm_path", std::string());
                    if (!tab.session || !pushedSnapshot(docId, r.result, snapshot) ||
                        aicm.empty()) {
                        if (then) then(false);
                        return;
                    }
                    tab.session->cachedAicmPath = aicm;
                    tab.session->cachedRevision = snapshot.revision;
                    tab.session->cachedMirror = std::move(snapshot);
                    tab.session->cachedDocMesh.reset();
                    st.cacheBackgroundDocMeshAsync(docId, tab.session->cachedRevision, aicm);
                    if (then) then(true);
                }, 15000);
}

}  // namespace core
