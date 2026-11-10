#include "engine_events.h"

#include <chrono>
#include <algorithm>
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
#include "cpp_core.h"
#include "tess_catchup.h"
#include "frame_stats.h"
#include "engine_backend.h"      // services/worker (include dir)
#include "ipc_client.h"          // services/ipc (include dir)
#include "../renderer.h"
#include "../render/aicm_loader.h"
#include "../ui/feature_tree.h"
#include "../ui/feature_tree_nested.h"
#include "../ui/property_manager.h"
#include "../ui/anim.h"
#include "../ui/reveng/reveng_internal.h"  // RevEngEvent / feedRevEngEvent / RevEngState
#include "../ui/manufacture/mfg_mirror.h"
#include "../ui/sketch_mode.h"
#include "../ui/sketch3d_mode.h"
#include "../ui/sim_manager.h"

namespace core {

using nlohmann::json;

namespace {
// Primary metadata round trips run on AsyncRpc's FIFO, never on the GL thread.
constexpr int kMetadataCallTimeoutMs = 5000;
constexpr int kAttachCallTimeoutMs = 10000;

// The user is mid-edit on the ACTIVE doc (open sketch or PM form) — the same
// safety gate pollAiCadHandoff used: never yank geometry out from under them.
bool userMidEdit(AppState& st) {
    return (st.sketchMode && st.sketchMode->active()) ||
           (st.sketch3dMode && st.sketch3dMode->active()) ||
           (st.propertyManager && st.propertyManager->active());
}

bool tabMidEdit(const AppState::DocTab& tab) {
    return tab.session &&
           ((tab.session->sketch && tab.session->sketch->active()) ||
            (tab.session->pm && tab.session->pm->active()));
}


int tabIndexForDoc(AppState& st, const std::string& docId) {
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
        if (st.docTabs[i].docId == docId) return i;
    return -1;
}

// Automatic presentation never changes a user's current selection, including an
// empty tab. A deliberate open/switch is the only way to replace that view.
bool canFocusMirror(AppState& st, bool firstMirror) {
    return firstMirror && (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size());
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
int bootstrapStep = 0;
bool bootstrapInFlight = false;

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

// Explicitly presented or GUI-origin documents awaiting a safe reveal, and
// the latest unapplied change for an admitted document.
std::map<std::string, std::string> pendingOpen;
std::map<std::string, json> pendingChanged;

void rememberChanged(const std::string& docId, const json& data) {
    const auto it = pendingChanged.find(docId);
    if (it == pendingChanged.end() ||
        data.value("revision", -1L) >= it->second.value("revision", -1L))
        pendingChanged[docId] = data;
}
std::map<std::string, bool> pendingHumanDirty;
std::map<std::string, long> dirtyEventRevision;
std::map<std::string, long> dirtyEventMetadataRevision;
std::map<std::string, long> reattachPending;
std::set<std::string> reattachInFlight;
std::chrono::steady_clock::time_point lastReattachTry{};
std::map<std::string, int> userOpensInFlight;
// Presentations already in the event queue admit subsequent revisions even
// before the budgeted dispatcher has handled the presentation itself.
std::set<std::string> queuedPresentations;
std::set<std::string> pendingExpiredHolds;
std::map<std::string, uint64_t> attachInFlight;
std::set<std::string> attachedPendingReveal;
uint64_t nextAttach = 0;
std::set<std::string> compatResyncInFlight;
struct PendingClose {
    std::string docId, title;
    bool detachOnly;
    uint64_t generation;
};
std::deque<PendingClose> pendingClose;
bool closeInFlight = false;
std::deque<json> pendingEvents;
std::map<std::string, json*> latestChanged;
int attachesThisFrame = 0;

// The in-process engine has the same cache-refresh sink as hostd, but no socket
// event stream. Never touch AppState from the engine/verification thread.
struct InProcessRefreshQueue {
    std::mutex mutex;
    std::deque<json> events;
    bool enabled = false;
    bool installed = false;
    bool attempted = false;
};

InProcessRefreshQueue& inProcessRefreshQueue() {
    // Verification callbacks may already have captured the sink at teardown.
    static auto* queue = new InProcessRefreshQueue;
    return *queue;
}

void inProcessRefresh(const char* docId, const char* responseJson, void*) {
    if (!docId || !*docId || !responseJson) return;
    try {
        json data = json::parse(responseJson, nullptr, false);
        if (!data.is_object()) return;
        data["doc_id"] = docId;
        data["source_kind"] = "system";
        data["method"] = "assembly_source_refresh";
        if (data.contains("tess") && data["tess"].is_object() &&
            data["tess"].contains("aicm_path"))
            data["aicm_path"] = data["tess"]["aicm_path"];
        auto& queue = inProcessRefreshQueue();
        {
            std::lock_guard<std::mutex> lock(queue.mutex);
            if (!queue.enabled) return;
            queue.events.push_back({{"event", "doc.changed"}, {"data", std::move(data)}});
        }
        ui::anim::wakeLoop();
    } catch (const std::exception& e) {
        std::fprintf(stderr, "[engine] cache refresh enqueue failed: %s\n", e.what());
    } catch (...) {
        std::fprintf(stderr, "[engine] cache refresh enqueue failed\n");
    }
}

bool closePending(const std::string& docId) {
    for (const auto& close : pendingClose)
        if (close.docId == docId) return true;
    return false;
}

// The part an AI edit presented last. A newer one replaces the older mirrors (see
// pruneOlderAiParts), and the viewport follows it unless the user's own work is in the way.
// Parts the pruning closed are remembered apart from the ones the user closed by hand: a later
// AI edit brings a pruned part back, a part the user closed stays closed.
std::set<std::string> focusWanted;
std::set<std::string> prunedAiParts;
std::string latestPresented;  // the doc id of the newest doc.presented
std::set<std::string> presentedUnopened;  // presented by an event, no tab opened for it yet
std::chrono::steady_clock::time_point lastAiFocus{};
constexpr std::chrono::milliseconds kAiFocusMinGap{1500};

// Replacing the view with the AI's part never disrupts unsaved work: an open sketch or PM form,
// edits not yet saved, a drawing or ECAD editor. A clean part (the user's own included) or an
// empty workspace is a view, not work, so the AI's part takes the viewport.
bool canFocusAiPart(AppState& st, bool rateLimited) {
    if (userMidEdit(st) || crashLoopBraked) return false;
    if (rateLimited && std::chrono::steady_clock::now() - lastAiFocus < kAiFocusMinGap)
        return false;
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return true;
    const AppState::DocTab& tab = st.docTabs[st.activeTab];
    return tab.kind == AppState::DocTab::TabKind::Part && !tab.dirty && !tabMidEdit(tab);
}

// Only the newest AI part stays open: older mirrors of AI work the user has not touched are
// released (detach only, the document lives on and is saved with the mission), so the workspace
// holds the current part instead of one tab per attempt. The tab on screen is left for the
// focus switch to replace. Closed parts stay reachable from the Artifacts panel.
void pruneOlderAiParts(AppState& st, const std::string& keepDocId) {
    for (int i = (int)st.docTabs.size() - 1; i >= 0; --i) {
        const AppState::DocTab& tab = st.docTabs[i];
        if (tab.docId.empty() || tab.docId == keepDocId || !tab.aiHandoff || tab.userAdopted ||
            tab.dirty || tab.kind != AppState::DocTab::TabKind::Part || i == st.activeTab ||
            tabMidEdit(tab))
            continue;
        prunedAiParts.insert(tab.docId);
        st.closeDocTab(i, true);
    }
}

void admitPrunedPart(AppState& st, const std::string& docId) {
    if (!prunedAiParts.erase(docId)) return;
    st.dismissedMirrorDocs.erase(docId);
    // A close that has not gone out yet is superseded; one in flight finishes first and the
    // reveal retries behind it.
    for (auto it = pendingClose.begin(); it != pendingClose.end();) {
        if (it->docId == docId && !(closeInFlight && it == pendingClose.begin()))
            it = pendingClose.erase(it);
        else ++it;
    }
}

// Brings the AI's part onto the viewport once its mirror tab exists. The snapshot is primed
// first (lock-free), so the switch binds from the parked state instead of a doc_activate that
// would queue behind the agent's writes.
void focusAiPart(AppState& st, const std::string& docId) {
    const int idx = tabIndexForDoc(st, docId);
    if (idx < 0) return;
    if (idx == st.activeTab) { pruneOlderAiParts(st, docId); return; }
    if (!canFocusAiPart(st, true)) { pruneOlderAiParts(st, docId); return; }
    const int activeBefore = st.activeTab;
    const std::string activeDocBefore =
        activeBefore >= 0 && activeBefore < (int)st.docTabs.size()
            ? st.docTabs[activeBefore].docId : std::string();
    core::primeMirrorFromSnapshot(st, docId,
                                  [&st, docId, activeBefore, activeDocBefore](bool) {
        // The user can switch tabs or start an edit while the snapshot is in flight: their
        // intent wins.
        if (st.activeTab != activeBefore ||
            (activeBefore >= 0 &&
             (activeBefore >= (int)st.docTabs.size() ||
              st.docTabs[activeBefore].docId != activeDocBefore)) ||
            !canFocusAiPart(st, false))
            return;
        const int i = tabIndexForDoc(st, docId);
        if (i < 0 || i == st.activeTab) return;
        lastAiFocus = std::chrono::steady_clock::now();
        st.switchToTab(i);
        pruneOlderAiParts(st, docId);
    });
}

struct RecoveredInfo {
    bool humanDirty;
    int revision;
    std::string path;
    bool diskConflict;
    json observedStamp;
};
std::map<std::string, RecoveredInfo> recoveredDocs;
// Obligations cover active and parked tabs until the matching mesh commits.
TessCatchup tessCatchup;

void applyDirtyMetadata(AppState& st, const std::string& docId, const json& data) {
    if (!data.contains("human_dirty") || !data["human_dirty"].is_boolean()) return;
    const long revision = data.value("revision", -1L);
    const long metadataRevision = data.value("metadata_revision", -1L);
    const auto seen = dirtyEventRevision.find(docId);
    const auto seenMeta = dirtyEventMetadataRevision.find(docId);
    if (seen != dirtyEventRevision.end() &&
        (revision < seen->second ||
         (revision == seen->second && metadataRevision >= 0 &&
          seenMeta != dirtyEventMetadataRevision.end() && metadataRevision < seenMeta->second)))
        return;
    dirtyEventRevision[docId] = revision;
    if (metadataRevision >= 0) dirtyEventMetadataRevision[docId] = metadataRevision;
    const bool humanDirty = data["human_dirty"].get<bool>();
    const int idx = tabIndexForDoc(st, docId);
    if (idx < 0) {
        if (pendingOpen.count(docId) || userOpensInFlight.count(docId))
            pendingHumanDirty[docId] = humanDirty;
    } else if (humanDirty) {
        auto& tab = st.docTabs[idx];
        const int metaRevision = data.value("metadata_revision", -1);
        if ((!tab.dirty || data.value("source_kind", std::string()) == "gui") &&
            metaRevision > tab.lastHumanMetadataRevision) {
            tab.lastHumanMetadataRevision = metaRevision;
            ++tab.humanEditGeneration;
        }
        st.applyHumanDirty(docId, true);
    } else if (data.value("saved_metadata_revision", -1) >=
               st.docTabs[idx].lastHumanMetadataRevision) {
        st.markSaved(docId, data.value("saved_revision", -1), {});
    }
}

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

// Open an admitted document as a read-only mirror without disrupting user content.
bool openMirrorTab(AppState& st, const std::string& docId, const std::string& name = {}) {
    if (docId.rfind("doc-cpp", 0) != 0) return true;  // parts/assemblies only
    if (userOpensInFlight.count(docId)) {
        if (!name.empty() || !pendingOpen.count(docId)) pendingOpen[docId] = name;
        return true;
    }
    if (const int existing = tabIndexForDoc(st, docId); existing >= 0) {
        const auto recovered = recoveredDocs.find(docId);
        if (recovered != recoveredDocs.end()) {
            if (!recovered->second.path.empty()) st.docTabs[existing].path = recovered->second.path;
            if (recovered->second.humanDirty) st.markHumanEdit(docId, recovered->second.revision);
            if (recovered->second.humanDirty && recovered->second.diskConflict) {
                st.docTabs[existing].diskConflict = true;
                st.docTabs[existing].diskConflictStamp = recovered->second.observedStamp;
            }
            recoveredDocs.erase(recovered);
        }
        pendingOpen.erase(docId);  // a deferred reveal is satisfied — don't retry every frame
        attachedPendingReveal.erase(docId);
        if (focusWanted.erase(docId)) focusAiPart(st, docId);
        return true;
    }
    // The user closed this mirror by hand. The agent keeps building (hostd degraded
    // that close to a detach), but re-revealing the tab on its next edit made the
    // close look broken. Stay closed until the user opens it again from Artifacts.
    if (st.dismissedMirrorDocs.count(docId)) {
        focusWanted.erase(docId);
        pendingOpen.erase(docId);
        attachedPendingReveal.erase(docId);
        pendingHumanDirty.erase(docId);
        pendingChanged.erase(docId);
        return true;
    }
    // A part the AI moved on from before its tab ever opened is not worth opening: the newest
    // presentation is the part on show. It stays reachable from the Artifacts panel.
    if (!latestPresented.empty() && docId != latestPresented && prunedAiParts.count(docId) == 0 &&
        presentedUnopened.count(docId)) {
        presentedUnopened.erase(docId);
        prunedAiParts.insert(docId);
        st.dismissedMirrorDocs[docId] = name.empty() ? std::string("AI part") : name;
        pendingOpen.erase(docId);
        attachedPendingReveal.erase(docId);
        pendingChanged.erase(docId);
        return true;
    }
    if (userMidEdit(st)) {                       // defer the reveal, never disrupt
        // Keep a name we already know if this retry arrived without one.
        if (!name.empty() || !pendingOpen.count(docId)) pendingOpen[docId] = name;
        return true;
    }
    auto* c = engine::client();
    if (!c) return false;
    if (!name.empty() || !pendingOpen.count(docId)) pendingOpen[docId] = name;
    if (closePending(docId) || attachInFlight.count(docId)) return false;
    if (!attachedPendingReveal.count(docId)) {
        if (attachesThisFrame >= 2) { ui::anim::requestFrames(); return false; }
        ++attachesThisFrame;
        const uint64_t generation = c->reconnectGeneration();
        const uint64_t request = ++nextAttach;
        attachInFlight[docId] = request;
        st.rpc.callPrimary("attach_doc", json{{"doc_id", docId}, {"mode", "ro"}},
                           [&st, docId, generation, request](const RpcResult& r) {
            const auto inFlight = attachInFlight.find(docId);
            if (inFlight == attachInFlight.end() || inFlight->second != request) return;
            attachInFlight.erase(inFlight);
            auto* client = engine::client();
            if (!client || client->reconnectGeneration() != generation ||
                !pendingOpen.count(docId) || closePending(docId)) return;
            if (!r.ok) {
                st.addLog("[daemon] attach_doc failed for " + docId + ": " + r.err);
                if (r.result.is_null())
                    st.toast(ui::ToastLevel::Warn, "Couldn't attach live document — retrying");
                else {
                    st.toast(ui::ToastLevel::Warn, "Couldn't attach live document: " + r.err);
                    pendingOpen.erase(docId);
                }
                return;
            }
            if (tabIndexForDoc(st, docId) >= 0) { pendingOpen.erase(docId); return; }
            attachedPendingReveal.insert(docId);
            lastMirrorRetry = {};
            ui::anim::requestFrames();
        }, kAttachCallTimeoutMs);
        return false;
    }
    attachedPendingReveal.erase(docId);

    bool anyMirror = false;
    for (const auto& t : st.docTabs)
        if (t.aiHandoff) anyMirror = true;
    // An AI edit presented this part: it takes the viewport (and the older AI parts are
    // closed). A mirror revealed for any other reason keeps the old rule: only into an empty
    // workspace.
    const bool presentedByAi = focusWanted.count(docId) != 0;
    const bool takeFocus = presentedByAi ? canFocusAiPart(st, true)
                                         : canFocusMirror(st, !anyMirror);
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
        st.rpc.callPrimary("doc_tree", json{{"doc_id", docId}},
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
    auto recovered = recoveredDocs.find(docId);
    if (recovered != recoveredDocs.end()) t.path = recovered->second.path;
    t.handoffDocId = docId;
    st.docTabs.push_back(std::move(t));
    if (recovered != recoveredDocs.end()) {
        if (recovered->second.humanDirty)
            st.markHumanEdit(docId, recovered->second.revision);
        if (recovered->second.humanDirty && recovered->second.diskConflict) {
            st.docTabs.back().diskConflict = true;
            st.docTabs.back().diskConflictStamp = recovered->second.observedStamp;
        }
        recoveredDocs.erase(recovered);
        pendingHumanDirty.erase(docId);
    } else if (auto dirty = pendingHumanDirty.find(docId); dirty != pendingHumanDirty.end()) {
        st.applyHumanDirty(docId, dirty->second);
        pendingHumanDirty.erase(dirty);
    }
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
                                      [&st, docId, activeBefore, activeDocBefore, anyMirror, presentedByAi](bool) {
            // A user can switch or edit tabs while the snapshot request is in
            // flight. Never steal the viewport after that change of intent.
            if (userMidEdit(st) || crashLoopBraked || st.activeTab != activeBefore ||
                (activeBefore >= 0 &&
                 (activeBefore >= (int)st.docTabs.size() ||
                  st.docTabs[activeBefore].docId != activeDocBefore)))
                return;
            const int i = tabIndexForDoc(st, docId);
            if (i < 0 || i == st.activeTab) return;
            if (presentedByAi ? canFocusAiPart(st, false) : canFocusMirror(st, !anyMirror)) {
                if (presentedByAi) lastAiFocus = std::chrono::steady_clock::now();
                st.switchToTab(i);
                if (presentedByAi) pruneOlderAiParts(st, docId);
            }
        });
        st.toast(ui::ToastLevel::Info, "Presented part — live updates are enabled");
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
    presentedUnopened.erase(docId);
    if (presentedByAi) {
        focusWanted.erase(docId);
        if (!takeFocus) {
            // The viewport is busy with the user's own work: say where the part is.
            st.toast(ui::ToastLevel::Info, "The AI's part is open in its own tab");
            pruneOlderAiParts(st, docId);
        }
    }
    return true;
}

void applyChangedActive(AppState& st, const std::string& docId, const json& d);
void handleEvent(AppState& st, json evt);

bool hasUsableTessellation(const json& data) {
    if (!data.is_object() || data.contains("mesh_error")) return false;
    const auto tess = data.find("tess");
    if (tess != data.end() && (!tess->is_object() || tess->contains("mesh_error")))
        return false;
    const json* path = nullptr;
    if (auto p = data.find("aicm_path"); p != data.end()) path = &*p;
    else if (tess != data.end()) {
        auto p = tess->find("aicm_path");
        if (p != tess->end()) path = &*p;
    }
    if (!path || !path->is_string()) return false;
    if (!path->get_ref<const std::string&>().empty()) return true;
    return tess != data.end() && tess->contains("bodies") &&
           (*tess)["bodies"].is_array() && (*tess)["bodies"].empty();
}

// Fetch a complete current tree and tessellation; the obligation survives failures,
// tab switches and editing until the corresponding mesh actually commits.
void fetchTess(AppState& st, const std::string& docId) {
    st.rpc.call("doc_activate", json{{"doc_id", docId}, {"origin", "system"}},
                [&st, docId](const core::RpcResult& r) {
        if (!tessCatchup.pending(docId)) return;
        const json& res = r.result;
        const bool usable = r.ok && res.is_object() && !res.contains("code") &&
            res.contains("revision") && res["revision"].is_number_integer() &&
            res["revision"].get<long>() >= tessCatchup.wanted(docId) &&
            res.contains("tree") && res["tree"].is_object() &&
            res.contains("tess") && res["tess"].is_object() &&
            res["tess"].contains("aicm_path") && hasUsableTessellation(res);
        tessCatchup.fetched(docId, usable);
        if (!usable) {
            st.addLog("[daemon] viewport mesh catch-up failed for " + docId + ": " +
                      (r.ok && res.is_object() ? res.value("message", std::string("mesh unavailable"))
                                               : r.err));
            ui::anim::requestFrames();
            return;
        }
        json data = res;
        data["doc_id"] = docId;
        data["aicm_path"] = res["tess"]["aicm_path"];
        handleEvent(st, {{"event", "doc.changed"}, {"data", std::move(data)}});
    });
}

void tickTess(AppState& st) {
    for (const auto& tab : st.docTabs) {
        if (tab.docId.empty() || reattachPending.count(tab.docId) ||
            reattachInFlight.count(tab.docId) || tabMidEdit(tab) ||
            (tab.docId == st.docMirror.docId() && tab.docId ==
                 (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()
                      ? st.docTabs[st.activeTab].docId : std::string()) && userMidEdit(st)) ||
            pendingChanged.count(tab.docId)) continue;
        if (tessCatchup.beginFetch(tab.docId)) fetchTess(st, tab.docId);
    }
    if (!tessCatchup.empty()) ui::anim::requestFrames();
}

// Apply a pushed foreign revision to the active live view without a blocking worker round-trip.
// The event carries the immutable tree/owner map and AICM path; disk parse runs on the mesh loader.
void applyChangedActive(AppState& st, const std::string& docId, const json& d) {
    const int idx = tabIndexForDoc(st, docId);
    if (idx < 0 || idx != st.activeTab ||
        (st.docMirror.docId() == docId &&
         d.value("revision", -1L) < (long)st.docMirror.revision())) return;
    AppState::DocTab& t = st.docTabs[idx];
    DocMirrorSnapshot snapshot;
    if (!pushedSnapshot(docId, d, snapshot)) {
        // Old daemons push only revision/path. Fetch a complete current response
        // off-thread rather than resyncing through the primary mutex on GL.
        pendingChanged[docId] = d;
        if (compatResyncInFlight.insert(docId).second) {
            st.rpc.call("doc_activate", json{{"doc_id", docId}, {"origin", "system"}},
                        [&st, docId](const RpcResult& r) {
                compatResyncInFlight.erase(docId);
                if (!r.ok || !r.result.is_object() || !r.result.contains("tree")) return;
                const auto pending = pendingChanged.find(docId);
                if (pending == pendingChanged.end() || userMidEdit(st) ||
                    st.docMirror.docId() != docId) return;
                const long revision = r.result.value("revision", -1L);
                if (revision < pending->second.value("revision", -1L) ||
                    revision < (long)st.docMirror.revision()) return;
                json current = r.result;
                current["doc_id"] = docId;
                if (current.contains("tess") && current["tess"].is_object() &&
                    current["tess"].contains("aicm_path") &&
                    !current["tess"].contains("mesh_error"))
                    current["aicm_path"] = current["tess"]["aicm_path"];
                applyChangedActive(st, docId, current);
            });
        }
        return;
    }
    if (st.renderer) st.renderer->discardCachedMeshes(docId);
    t.meshCacheInvalidated = true;
    if (t.session) t.session->opsCatchupUncertain = false;
    st.docMirror.restoreSnapshot(snapshot);
    st.syncActiveTabTitle();

    tessCatchup.require(docId, snapshot.revision);
    if (!hasUsableTessellation(d)) {
        if (t.session) {
            t.session->cachedAicmPath.clear();
            t.session->cachedRevision = snapshot.revision;
            t.session->cachedMirror = snapshot;
        }
        pendingChanged.erase(docId);
        ui::anim::requestFrames();
        return;
    }
    const std::string aicm = d.value("aicm_path", std::string());
    if (t.session) {
        t.session->cachedAicmPath = aicm;
        t.session->cachedRevision = snapshot.revision;
        t.session->cachedMirror = std::move(snapshot);
        t.session->cachedDocMesh.reset();
    }
    if (!aicm.empty()) {
        tessCatchup.loading(docId, st.docMirror.revision());
        st.loadDocMeshAsync(aicm, false, true, true, true);
    } else {
        st.unloadDocMesh();
        if (st.renderer) st.renderer->setActiveMeshIdentity(docId, st.docMirror.revision());
        if (t.session) t.session->cachedDocMesh = std::make_shared<render::DocMesh>();
        t.meshCacheInvalidated = false;
        st.committedMeshDocId = docId;
        st.committedMeshRevision = st.docMirror.revision();
        tessCatchup.finished(docId, st.docMirror.revision(), true);
    }
    pendingChanged.erase(docId);
}

// An expired edit-hold while OUR PropertyManager previews that doc: cancel the
// preview — but only undo if the doc revision is still the one the PM's
// checkpoint lineage expects (mirror in step with the engine). If a foreign
// edit already landed, undoing would destroy it: skip the undo, close the form,
// and resync instead (plan: "verify revision parentage before undo").
void handleOwnHoldExpired(AppState& st, const std::string& docId) {
    if (!st.propertyManager || !st.propertyManager->active() ||
        st.docMirror.docId() != docId || !pendingExpiredHolds.insert(docId).second) return;
    auto* manager = st.propertyManager;
    const auto lifetime = manager->editSessionLifetime();
    st.rpc.callPrimary("doc_tree", json{{"doc_id", docId}},
                       [&st, docId, manager, lifetime](const RpcResult& r) {
        pendingExpiredHolds.erase(docId);
        if (lifetime.expired() || st.propertyManager != manager || !manager->active() ||
            st.docMirror.docId() != docId) return;
        if (!r.ok) {
            st.addLog("[daemon] doc_tree revision check failed for " + docId + ": " + r.err);
            manager->closeIfActive();
            st.toast(ui::ToastLevel::Warn,
                     "Edit hold expired — revision unavailable; edit form closed without undo");
            return;
        }
        const long revision = r.result.value("revision", -1L);
        if (revision >= 0 && revision == (long)st.docMirror.revision()) {
            manager->cancelEditsAsync(st);
            st.toast(ui::ToastLevel::Warn, "Edit hold expired — preview cancelled");
        } else {
            manager->closeIfActive();
            st.rpc.call("doc_activate", json{{"doc_id", docId}, {"origin", "system"}},
                        [&st, docId](const RpcResult& active) {
                if (!active.ok || st.docMirror.docId() != docId) return;
                std::string aicm, err;
                if (st.docMirror.adoptActivateResponse(docId, active.result, aicm, err) &&
                    !aicm.empty()) st.loadDocMeshAsync(aicm);
            });
            st.toast(ui::ToastLevel::Warn,
                     "Edit hold expired and the document moved — preview dropped, view resynced");
        }
    }, kMetadataCallTimeoutMs);
}

// Only an exact run id can identify an event on the daemon event connection.
// Online handles are installed before dispatch; native jobs stream on their own callback.
JobState* runForEvent(AppState& st, const std::string& runId) {
    if (runId.empty()) return nullptr;
    for (auto& kv : st.runs) {
        JobState* run = kv.second.get();
        if (!run) continue;
        std::lock_guard<std::mutex> jlk(run->mtx);
        if (run->runId == runId) return run;
    }
    return nullptr;
}

void handleEvent(AppState& st, json evt) {
    const std::string name = evt.value("event", std::string());
    static const json empty = json::object();
    const json& d = evt.contains("data") && evt["data"].is_object() ? evt["data"] : empty;
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
#ifdef __EMSCRIPTEN__
        // Cells broadcast each event to all of an actor's sockets. The owning
        // turn's dedicated callback already consumes it with a strict run-id
        // check; enqueuing the primary socket's copy would render it twice.
        return;
#endif
        JobState* job = runForEvent(st, d.value("run_id", std::string()));
        if (!job) return;  // unknown or ambiguous run — never guess a target conversation
        AgentEvent e;
        const std::string origKind = d.value("kind", std::string());
        e.kind = "orch_" + origKind;
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
        e.data = std::make_shared<const json>(std::move(evt["data"]));
        {
            std::lock_guard<std::mutex> lk(job->evMtx);
            job->events.push_back(std::move(e));
        }
        return;
    }

    if (name == "doc.opened") {
        if (!docId.empty() && d.value("origin_kind", std::string()) == "gui") {
            openMirrorTab(st, docId, eventDocName(d));
        }
        return;
    }
    if (name == "doc.presented") {
        queuedPresentations.erase(docId);
        if (!docId.empty()) {
            // The newest presentation is the one that takes the viewport.
            admitPrunedPart(st, docId);
            focusWanted.clear();
            focusWanted.insert(docId);
            latestPresented = docId;
            if (tabIndexForDoc(st, docId) < 0) presentedUnopened.insert(docId);
            openMirrorTab(st, docId, eventDocName(d));
        }
        return;
    }
    if (name == "doc.disk_conflict") {
        const int idx = tabIndexForDoc(st, docId);
        if (idx >= 0) {
            st.docTabs[idx].diskConflict = true;
            st.docTabs[idx].overwriteDiskConflict = false;
            st.docTabs[idx].diskConflictStamp = d.value("observed_stamp", json());
            st.docTabs[idx].conflictPath.clear();
        }
        return;
    }
    if (name == "doc.changed" || name == "doc.changed.notice" || name == "doc.cache_refreshed") {
        if (docId.empty()) return;
        int idx = tabIndexForDoc(st, docId);
        // A hidden or dismissed working document must not retain snapshots,
        // produce toasts, or trigger an attach/tessellation.
        if (idx < 0 && ((!pendingOpen.count(docId) && !userOpensInFlight.count(docId)) ||
                        st.dismissedMirrorDocs.count(docId)))
            return;
        applyDirtyMetadata(st, docId, d);
        if (idx >= 0 && !st.docTabs[idx].path.empty())
            ui::invalidateFeatureTreeNestedDocument(st.docTabs[idx].path);
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
            if (idx >= 0) st.docTabs[idx].historyCleared = false;
        }
        if (name == "doc.changed.notice") {
            const std::string method = d.value("method", std::string());
            const auto meta = dirtyEventMetadataRevision.find(docId);
            const long noticeMeta = d.value("metadata_revision", -1L);
            const bool current = meta == dirtyEventMetadataRevision.end() || noticeMeta < 0 ||
                                 noticeMeta >= meta->second;
            if (idx >= 0 && current && (method == "doc_set_name" || method == "doc_set_meta")) {
                if (method == "doc_set_name" && d.contains("name") && d["name"].is_string()) {
                    st.docTabs[idx].title = d["name"].get<std::string>();
                    if (st.docTabs[idx].aiHandoff) st.docTabs[idx].handoffName = st.docTabs[idx].title;
                }
                tessCatchup.require(docId, d.value("revision", 0L));
            }
            return;
        }
        if (idx < 0) {
            rememberChanged(docId, d);
            return;
        }
        const auto deferred = pendingChanged.find(docId);
        if (deferred != pendingChanged.end() &&
            d.value("revision", -1L) < deferred->second.value("revision", -1L)) return;
        if (idx != st.activeTab) {
            AppState::DocTab& tab = st.docTabs[idx];
            if (tab.session && d.value("revision", -1L) < tab.session->cachedRevision) return;
            tab.aiPulse = true;  // background activity remains visible in the tab strip
            tab.lastAgentEditAt = std::chrono::steady_clock::now();
            // A parked sketch/PM is still an in-progress edit. Keep its old mirror and
            // mesh until the user returns and finishes, just as for an active edit.
            if (tabMidEdit(tab)) {
                rememberChanged(docId, d);
                return;
            }
            tab.meshCacheInvalidated = true;
            if (st.renderer) st.renderer->discardCachedMeshes(docId);
            DocMirrorSnapshot snapshot;
            const std::string aicm = d.value("aicm_path", std::string());
            if (tab.session && pushedSnapshot(docId, d, snapshot)) {
                const int revision = snapshot.revision;
                tessCatchup.require(docId, revision);
                tab.session->cachedRevision = revision;
                tab.session->opsCatchupUncertain = false;
                tab.session->cachedMirror = std::move(snapshot);
                if (!hasUsableTessellation(d)) {
                    tab.session->cachedAicmPath.clear();
                } else if (aicm.empty()) {
                    tab.session->cachedAicmPath.clear();
                    tab.session->cachedDocMesh = std::make_shared<render::DocMesh>();
                    tab.meshCacheInvalidated = false;
                    tessCatchup.finished(docId, revision, true);
                } else {
                    tab.session->cachedAicmPath = aicm;
                    tessCatchup.loading(docId, revision);
                    st.cacheBackgroundDocMeshAsync(docId, revision, aicm);
                }
                if (deferred != pendingChanged.end() &&
                    deferred->second.value("revision", -1L) <= revision)
                    pendingChanged.erase(deferred);
            } else {
                tessCatchup.require(docId, d.value("revision", 0L));
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
            rememberChanged(docId, d);
            st.docTabs[idx].aiPulse = true;
            st.docTabs[idx].lastAgentEditAt = std::chrono::steady_clock::now();
            return;
        }
        applyChangedActive(st, docId, d);
        return;
    }
    if (name == "doc.studies_changed") {
        if (docId.empty()) return;
        const int idx = tabIndexForDoc(st, docId);
        if (idx < 0) return;
        applyDirtyMetadata(st, docId, d);
        if (st.docTabs[idx].session && st.docTabs[idx].session->sim)
            st.docTabs[idx].session->sim->markStudiesStale();
        tessCatchup.require(docId, d.value("revision", 0L));
        return;
    }
    if (name == "doc.closed") {
        const std::string reason = d.value("reason", std::string());
        const int idx = tabIndexForDoc(st, docId);
        // Last-writer-detached is a custody notice; the document remains live.
        if (reason == "last_writer_detached") {
            if (idx >= 0) {
                st.docTabs[idx].aiPulse = false;
                st.docTabs[idx].agentDone = true;
            }
            return;
        }
        pendingOpen.erase(docId);
        pendingChanged.erase(docId);
        pendingHumanDirty.erase(docId);
        dirtyEventRevision.erase(docId);
        dirtyEventMetadataRevision.erase(docId);
        reattachPending.erase(docId);
        reattachInFlight.erase(docId);
        tessCatchup.forget(docId);
        attachInFlight.erase(docId);
        attachedPendingReveal.erase(docId);
        pendingExpiredHolds.erase(docId);
        queuedPresentations.erase(docId);
        if (idx < 0) return;
        if (!st.docTabs[idx].aiHandoff || st.docTabs[idx].userAdopted) {
            // A tab the user opened themselves, or an AI mirror the user ADOPTED by editing:
            // it's user-owned now. Never auto-close it — the user decides its fate (and the
            // quit warning covers it).
            st.docTabs[idx].aiPulse = false;
            st.docTabs[idx].agentDone = true;
            st.addLog("[daemon] doc.closed for user-owned tab " + docId + " (reason " + reason + ")");
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
        if (tabIndexForDoc(st, docId) < 0) return;
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

void tryBootstrap(AppState& st, chudware_ipc::EngineClient& c) {
    if (bootstrapInFlight) return;
    bootstrapInFlight = true;
    const uint64_t generation = c.reconnectGeneration();
    const int step = bootstrapStep;
    const char* method = step == 0 ? "subscribe" : step == 1 ? "doc_list" : "recovery_list";
    st.rpc.callPrimary(method, step == 0 ? json{{"doc_ids", "*"}} : json::object(),
                       [&st, generation, step](const RpcResult& r) {
        auto* client = engine::client();
        if (!client || generation != bootstrapGen ||
            generation != client->reconnectGeneration()) return;
        bootstrapInFlight = false;
        if (!r.ok && !client->connected()) {
            reportBootstrapTransportFailure(st, "bootstrap transport failure: " + r.err);
            bootstrapStep = 0;
            return;
        }
        if (step == 0) {
            if (!r.ok) { st.addLog("[daemon] subscribe failed: " + r.err); return; }
            bootstrapStep = 1;
        } else if (step == 1) {
            if (!r.ok && r.result.is_object() &&
                r.result.value("code", std::string()) == "loading") return;
            if (!r.ok || !r.result.is_object() || !r.result.contains("docs") ||
                !r.result["docs"].is_array()) {
                st.addLog("[daemon] doc_list failed: " + r.err);
                return;
            }
            const json& list = r.result;
            if (r.ok && list.is_object() && list.contains("docs") && list["docs"].is_array()) {
                std::set<std::string> liveIds;
                // Presented AI parts are not all reopened on a reconnect or reload: only the
                // newest presentation is the one kept open.
                long newestPresented = 0;
                for (const auto& doc : list["docs"])
                    if (doc.is_object() && doc.value("origin_kind", std::string()) != "gui")
                        newestPresented = std::max(newestPresented, doc.value("presented_seq", 0L));
                for (const auto& doc : list["docs"]) {
                    const std::string id = doc.is_object() ? doc.value("doc_id", std::string())
                                                          : std::string();
                    if (id.empty()) continue;
                    liveIds.insert(id);
                    const bool guiOrigin = doc.value("origin_kind", std::string()) == "gui";
                    if (tabIndexForDoc(st, id) < 0 &&
                        (guiOrigin || (doc.value("presented", false) &&
                                       doc.value("presented_seq", 0L) == newestPresented)) &&
                        !st.dismissedMirrorDocs.count(id))
                        pendingOpen[id] = eventDocName(doc);
                    const int ti = tabIndexForDoc(st, id);
                    applyDirtyMetadata(st, id, doc);
                    if (ti >= 0) reattachPending[id] = doc.value("revision", 0L);
                }
                st.addLog("[daemon] bootstrap: " + std::to_string(liveIds.size()) +
                          " live doc(s), admitted documents queued for mirroring");
                // Reconcile one stale mirror at a time through the budgeted event queue.
                for (const auto& tab : st.docTabs) {
                    if (!tab.aiHandoff || tab.userAdopted || tab.docId.empty() ||
                        liveIds.count(tab.docId) || tab.dirty ||
                        (tab.docId == st.docMirror.docId() && userMidEdit(st))) continue;
                    latestChanged.clear();
                    pendingEvents.push_back({{"event", "doc.closed"},
                        {"data", {{"doc_id", tab.docId}, {"reason", "daemon"}}}});
                }
            }
            bootstrapStep = 2;
        } else {
#ifndef __EMSCRIPTEN__
            if (!r.ok || !r.result.is_object() || !r.result.contains("entries") ||
                !r.result["entries"].is_array()) {
                st.addLog("[daemon] recovery_list unavailable; retrying: " + r.err);
                return;
            }
#endif
            if (r.ok && r.result.is_object() && r.result.contains("entries") &&
                r.result["entries"].is_array() && !r.result["entries"].empty()) {
                st.recoveryPrompt.clear();
                for (const auto& e : r.result["entries"]) {
                    if (!e.is_object() || !e.value("human_dirty", false)) continue;
                    const std::string id = e.value("id", std::string());
                    if (!id.empty()) st.recoveryPrompt.push_back(
                        {id, e.value("name", id), true, e.value("revision", -1),
                         e.value("path", std::string())});
                }
                st.addLog("[daemon] " + std::to_string(st.recoveryPrompt.size()) +
                          " retired doc(s) from a previous session awaiting recovery");
            }
            bootstrapped = true;
            st.daemonEverConnected = true;
        }
        lastBootstrapTry = {};
        ui::anim::requestFrames();
    }, step == 2 ? kLongEngineCallTimeoutMs : kMetadataCallTimeoutMs);
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

void applyDeferredChange(AppState& st) {
    for (auto it = pendingChanged.begin(); it != pendingChanged.end(); ++it) {
        const int idx = tabIndexForDoc(st, it->first);
        if (idx < 0 || tabMidEdit(st.docTabs[idx]) ||
            (idx == st.activeTab && userMidEdit(st))) continue;
        json data = std::move(it->second);
        pendingChanged.erase(it);
        handleEvent(st, {{"event", "doc.changed"}, {"data", std::move(data)}});
        return;
    }
}

}  // namespace

void requireDocMesh(const std::string& docId, int revision) {
    tessCatchup.require(docId, revision);
    ui::anim::requestFrames();
}

void meshCatchupStarted(const std::string& docId, int revision) {
    tessCatchup.require(docId, revision);
    tessCatchup.loading(docId, revision);
}

void forgetDocMesh(const std::string& docId) {
    tessCatchup.forget(docId);
}

void meshCatchupFinished(const std::string& docId, int revision, bool success) {
    if (docId.empty() || revision < 0) return;
    tessCatchup.finished(docId, revision, success);
    if (!success) ui::anim::requestFrames();
}

void reloadDiskConflict(AppState& st, const std::string& docId) {
    const int idx = tabIndexForDoc(st, docId);
    if (idx < 0 || !st.docTabs[idx].diskConflict) return;
    if (st.hasPendingHumanMutations(docId) || tabMidEdit(st.docTabs[idx]) ||
        (idx == st.activeTab && userMidEdit(st))) {
        st.toast(ui::ToastLevel::Info, "Finish the current edit before reloading.");
        return;
    }
    const std::string legacyPath = st.docTabs[idx].conflictPath;
    if (!legacyPath.empty()) {
        ui::reloadLegacyHandoffConflict(st, docId, legacyPath);
        return;
    }
    st.beginHumanMutation(docId);
    st.rpc.callPrimary("doc_reload", {{"doc_id", docId}, {"origin", "system"}},
        [&st, docId](const RpcResult& r) {
            st.endHumanMutation(docId);
            const int tab = tabIndexForDoc(st, docId);
            if (tab < 0) return;
            if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
                st.toast(ui::ToastLevel::Error, "Reload failed: " +
                         (r.ok && r.result.is_object()
                              ? r.result.value("message", std::string("engine error")) : r.err));
                return;
            }
            st.docTabs[tab].diskConflict = false;
            st.docTabs[tab].overwriteDiskConflict = false;
            st.docTabs[tab].diskConflictStamp = nullptr;
            st.markSaved(docId, r.result.value("revision", -1), {});
            json data = r.result;
            data["doc_id"] = docId;
            data["method"] = "doc_reload";
            if (data.contains("tess") && data["tess"].is_object() &&
                data["tess"].contains("aicm_path") && !data["tess"].contains("mesh_error"))
                data["aicm_path"] = data["tess"]["aicm_path"];
            handleEvent(st, {{"event", "doc.changed"}, {"data", std::move(data)}});
        }, kLongEngineCallTimeoutMs);
}

void beginUserDocOpen(AppState& st, const std::string& docId) {
    ++userOpensInFlight[docId];
    st.dismissedMirrorDocs.erase(docId);
    pendingOpen.erase(docId);
    attachInFlight.erase(docId);
    attachedPendingReveal.erase(docId);
    // A close not yet sent is superseded by the user's deliberate reopen.
    for (auto it = pendingClose.begin(); it != pendingClose.end();) {
        if (it->docId == docId && !(closeInFlight && it == pendingClose.begin()))
            it = pendingClose.erase(it);
        else ++it;
    }
}

void endUserDocOpen(AppState& st, const std::string& docId) {
    auto it = userOpensInFlight.find(docId);
    if (it == userOpensInFlight.end() || --it->second != 0) return;
    userOpensInFlight.erase(it);
    if (tabIndexForDoc(st, docId) >= 0) pendingOpen.erase(docId);
    auto pending = pendingChanged.find(docId);
    if (pending == pendingChanged.end()) return;
    json data = std::move(pending->second);
    pendingChanged.erase(pending);
    if (tabIndexForDoc(st, docId) >= 0)
        handleEvent(st, {{"event", "doc.changed"}, {"data", std::move(data)}});
}

void queueMirrorClose(AppState& st, const std::string& docId,
                      const std::string& title, bool detachOnly) {
    if (docId.empty()) return;
    // Suppress even presentations arriving while the detach/discard RPC is queued.
    // A deliberate reopen clears this immediately and must not be undone by its reply.
    st.dismissedMirrorDocs[docId] = title;
    auto* c = engine::client();
    pendingClose.push_back({docId, title, detachOnly, c ? c->reconnectGeneration() : 0});
    pendingOpen.erase(docId);
    pendingChanged.erase(docId);
    attachInFlight.erase(docId);
    attachedPendingReveal.erase(docId);
    focusWanted.erase(docId);
    ui::anim::requestFrames();
    queuedPresentations.erase(docId);
}

void shutdownEngineEvents() {
    auto& queue = inProcessRefreshQueue();
    const bool installed = queue.installed;
    {
        std::lock_guard<std::mutex> lock(queue.mutex);
        queue.enabled = false;
        queue.events.clear();
    }
    if (installed) cpp_core().setCacheRefreshSink(nullptr, nullptr);
    queue.installed = false;
    queue.attempted = false;
    pendingChanged.clear();
}

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
    attachesThisFrame = 0;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(2);
    // Pulse timeout runs in BOTH daemon and legacy mode — the legacy .step handoff poller
    // (feature_tree.cpp pollAiCadHandoff) stamps the same pulse — so it sits ahead of the
    // daemon-only gate below, at the very top of the frame.
    agentEditPulseTimeoutTick(st);
    // The reveng journal tail is a LOCAL FILE read, not a daemon push, so it runs in both
    // daemon and legacy in-process mode — ahead of the daemon-only gate below.
    revengReconstructTick();
    framestats::Scope connectTiming("engine_connect");
    if (!engine::daemonMode()) {
        connectTiming.stop();
        auto& queue = inProcessRefreshQueue();
        if (st.workerStarted && !queue.attempted) {
            queue.attempted = true;
            {
                std::lock_guard<std::mutex> lock(queue.mutex);
                queue.enabled = true;
            }
            queue.installed = cpp_core().setCacheRefreshSink(&inProcessRefresh, nullptr);
            if (!queue.installed) {
                std::lock_guard<std::mutex> lock(queue.mutex);
                queue.enabled = false;
                st.addLog("[engine] in-process cache refresh sink unavailable");
            }
        }
        while (std::chrono::steady_clock::now() < deadline) {
            json event;
            {
                std::lock_guard<std::mutex> lock(queue.mutex);
                if (queue.events.empty()) break;
                event = std::move(queue.events.front());
                queue.events.pop_front();
            }
            handleEvent(st, std::move(event));
        }
        applyDeferredChange(st);
        tickTess(st);
        {
            std::lock_guard<std::mutex> lock(queue.mutex);
            if (!queue.events.empty()) ui::anim::requestFrames();
        }
        return;
    }
    connectTiming.stop();
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
    framestats::Scope connectionTiming("engine_connection");
    const engine::DaemonConnState conn = engine::maintainConnection();
    connectionTiming.stop();
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
    if (st.daemonUnreachable && c->reconnectGeneration() != bootstrapGen) {
        const bool braked = noteReconnectAndCheckBrake();
        // Re-arm once per connection, including while the crash-loop brake
        // keeps its sticky notice visible. Resetting every tick would restart
        // the asynchronous subscription/list/recovery chain before it advanced.
        bootstrapped = false;
        bootstrapStep = 0;
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
        bootstrapStep = 0;
        bootstrapInFlight = false;
        pendingEvents.clear();
        latestChanged.clear();
        queuedPresentations.clear();
        attachInFlight.clear();
        attachedPendingReveal.clear();
        reattachPending.clear();
        reattachInFlight.clear();
        dirtyEventRevision.clear();
        dirtyEventMetadataRevision.clear();
        lastBootstrapTry = {};  // retry immediately, don't wait out the backoff
    }

    if (!bootstrapped && !bootstrapInFlight) {
        const auto now = std::chrono::steady_clock::now();
        if (now - lastBootstrapTry >= std::chrono::milliseconds(500)) {
            lastBootstrapTry = now;
            tryBootstrap(st, *c);
        }
    }

    // Primary identity matters: finish deferred closes before reattaching that doc.
    if (!pendingClose.empty() && !closeInFlight &&
        std::chrono::steady_clock::now() < deadline) {
        const PendingClose close = pendingClose.front();
        if (close.detachOnly && close.generation != gen) {
            pendingClose.pop_front();  // the old connection's attachment is already gone
        } else {
            closeInFlight = true;
            const std::string method = close.detachOnly ? "detach_doc" : "doc_discard";
            st.rpc.callPrimary(method, {{"doc_id", close.docId}},
                               [&st, close, method](const RpcResult& r) {
                if (r.ok && r.result.is_object() && r.result.value("detached", false)) {
                    // Dismissal was recorded at close time; a user reopen may have
                    // cleared it since the request was sent.
                    if (r.result.contains("note"))
                        st.addLog("[tabs] " + close.docId + ": " +
                                  r.result.value("note", std::string("detached")));
                } else if (!r.ok) {
                    st.addLog("[tabs] " + method + " failed for " + close.docId + ": " + r.err);
                    st.toast(ui::ToastLevel::Warn,
                             "Document close not confirmed — check Agent Documents");
                }
                pendingClose.pop_front();
                closeInFlight = false;
                ui::anim::requestFrames();
            }, kAttachCallTimeoutMs);
        }
    }
    // Subscribe first, then reattach every already-visible tab and replay a
    // fresh full revision. The attach precedes the fetch on the primary lane.
    if (bootstrapStep >= 2 && pendingClose.empty() &&
        std::chrono::steady_clock::now() - lastReattachTry >= std::chrono::milliseconds(500)) {
        for (auto it = reattachPending.begin(); it != reattachPending.end();) {
            if (tabIndexForDoc(st, it->first) < 0) it = reattachPending.erase(it);
            else ++it;
        }
        int sent = 0;
        for (const auto& [id, revision] : reattachPending) {
            if (sent >= 2) break;
            const int idx = tabIndexForDoc(st, id);
            if (idx < 0 || reattachInFlight.count(id)) continue;
            const auto& tab = st.docTabs[idx];
            const std::string mode = tab.aiHandoff && !tab.userAdopted ? "ro" : "rw";
            reattachInFlight.insert(id);
            ++sent;
            st.rpc.callPrimary("attach_doc", {{"doc_id", id}, {"mode", mode}},
                [&st, id, revision, gen](const RpcResult& r) {
                    auto* client = engine::client();
                    if (!client || client->reconnectGeneration() != gen) return;
                    reattachInFlight.erase(id);
                    if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
                        st.addLog("[daemon] reattach failed for " + id + ": " + r.err);
                    } else {
                        applyDirtyMetadata(st, id, r.result);
                        reattachPending.erase(id);
                        if (tabIndexForDoc(st, id) >= 0)
                            tessCatchup.require(id, std::max(0L, r.result.value("revision", revision)));
                    }
                    ui::anim::requestFrames();
                }, kAttachCallTimeoutMs);
        }
        lastReattachTry = std::chrono::steady_clock::now();
    }

    // Superseded revisions keep notices in place; lifecycle pushes are barriers.
    const double remaining = std::chrono::duration<double, std::milli>(
        deadline - std::chrono::steady_clock::now()).count();
    if (remaining > 0) {
        for (json& event : c->drainEvents(std::min(1.0, remaining))) {
            const std::string name = event.value("event", std::string());
            if (name == "doc.opened" || name == "doc.closed" || name == "doc.presented")
                latestChanged.clear();
            if (name == "doc.presented" && event.contains("data") && event["data"].is_object()) {
                const std::string id = event["data"].value("doc_id", std::string());
                admitPrunedPart(st, id);
                if (!id.empty() && !st.dismissedMirrorDocs.count(id)) queuedPresentations.insert(id);
            }
            if ((name == "doc.changed" || name == "doc.cache_refreshed") &&
                event.contains("data") && event["data"].is_object()) {
                const std::string id = event["data"].value("doc_id", std::string());
                if (tabIndexForDoc(st, id) < 0 && !pendingOpen.count(id) &&
                    !queuedPresentations.count(id) && !userOpensInFlight.count(id)) continue;
                if (!id.empty()) {
                    const auto old = latestChanged.find(id);
                    if (old != latestChanged.end()) {
                        const long prior = (*old->second)["data"].value("revision", -1L);
                        if (event["data"].value("revision", -1L) < prior) {
                            event["event"] = "doc.changed.notice";
                            pendingEvents.push_back(std::move(event));
                            continue;
                        }
                        (*old->second)["event"] = "doc.changed.notice";
                    }
                    pendingEvents.push_back(std::move(event));
                    latestChanged[id] = &pendingEvents.back();
                    continue;
                }
            }
            pendingEvents.push_back(std::move(event));
        }
    }
    while (!pendingEvents.empty() && std::chrono::steady_clock::now() < deadline) {
        json event = std::move(pendingEvents.front());
        const std::string eventName = event.value("event", std::string());
        if ((eventName == "doc.changed" || eventName == "doc.cache_refreshed") &&
            event.contains("data") && event["data"].is_object()) {
            const auto it = latestChanged.find(event["data"].value("doc_id", std::string()));
            if (it != latestChanged.end() && it->second == &pendingEvents.front())
                latestChanged.erase(it);
        }
        pendingEvents.pop_front();
        handleEvent(st, std::move(event));
    }
    if (bootstrapped && c->connected() && !pendingOpen.empty() && !userMidEdit(st) &&
        std::chrono::steady_clock::now() - lastMirrorRetry >= std::chrono::milliseconds(500)) {
        auto it = pendingOpen.begin();
        while (it != pendingOpen.end() && attachesThisFrame < 2 &&
               std::chrono::steady_clock::now() < deadline) {
            const std::string id = it->first, name = it->second;
            ++it;  // openMirrorTab erases its own entry on success
            if (!openMirrorTab(st, id, name)) break;
        }
        lastMirrorRetry = attachesThisFrame >= 2 ? std::chrono::steady_clock::time_point{}
                                                : std::chrono::steady_clock::now();
    }
    if (std::chrono::steady_clock::now() < deadline) applyDeferredChange(st);
    tickTess(st);
    if (!pendingEvents.empty() || c->hasPendingEvents() || !pendingClose.empty() ||
        !pendingExpiredHolds.empty() || !reattachPending.empty() ||
        (!pendingOpen.empty() && !userMidEdit(st)))
        ui::anim::requestFrames();
}

void mirrorRecoveredDocs(AppState& st, const std::vector<std::string>& docIds,
                         bool humanDirty, int revision, const std::string& path, bool diskConflict,
                         const json& observedStamp) {
    if (!engine::daemonMode() || !engine::client()) return;
    for (const auto& id : docIds) {
        if (id.empty()) continue;
        if (humanDirty || !path.empty())
            recoveredDocs[id] = {humanDirty, revision, path, diskConflict, observedStamp};
        openMirrorTab(st, id);
    }
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
                    const auto pending = pendingChanged.find(docId);
                    if (pending != pendingChanged.end() &&
                        r.result.value("revision", -1L) < pending->second.value("revision", -1L)) {
                        if (then) then(false);
                        return;
                    }
                    // Replay through the same active/parked and mesh-obligation path.
                    json data = r.result;
                    data["doc_id"] = docId;
                    handleEvent(st, {{"event", "doc.changed"}, {"data", std::move(data)}});
                    if (then) then(true);
                }, 15000);
}

}  // namespace core
