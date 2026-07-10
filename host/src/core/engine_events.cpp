#include "engine_events.h"

#include <map>
#include <set>
#include <string>

#include <nlohmann/json.hpp>

#include "app_state.h"
#include "engine_backend.h"      // services/worker (include dir)
#include "ipc_client.h"          // services/ipc (include dir)
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

// One-time daemon bootstrap: subscribe to every doc's events + mirror docs that
// were already open in the shared engine before this GUI attached (list_docs at
// attach, per the plan). Retried while the engine reports "loading" — this runs
// on the GUI thread, so it must never block on the load.
bool bootstrapped = false;

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
    if (tabIndexForDoc(st, docId) >= 0) return;  // already visible (ours or a prior mirror)
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

    // Display name from the doc tree (doc.opened doesn't carry one). Read-only
    // + cheap; tolerate failure (title falls back until first activation).
    std::string name;
    {
        json tr;
        std::string te;
        if (c->call("doc_tree", json{{"doc_id", docId}}, tr, te) && tr.is_object()) {
            if (tr.contains("tree") && tr["tree"].is_object())
                name = tr["tree"].value("name", std::string());
            if (name.empty()) name = tr.value("name", std::string());
        }
    }

    AppState::DocTab t;
    t.kind = AppState::DocTab::TabKind::Part;
    t.docId = docId;
    t.title = name.empty() ? std::string("AI part") : name;
    t.aiHandoff = true;
    t.handoffDocId = docId;
    t.handoffName = name;
    st.docTabs.push_back(std::move(t));
    const int idx = (int)st.docTabs.size() - 1;
    st.addLog("[daemon] mirror tab opened for " + docId + (name.empty() ? "" : " ('" + name + "')"));
    if (takeFocus) {
        st.switchToTab(idx);  // activates: doc_rebuild (ro-allowed) -> tree + mesh
        st.toast(ui::ToastLevel::Info,
                 "Live view of the AI's part — it updates as it builds");
    } else {
        st.docTabs[idx].aiPulse = true;  // background reveal; switch-in activates
    }
    pendingOpen.erase(docId);
}

// Apply the latest foreign doc.changed for the ACTIVE tab: refresh the mirror
// (cheap doc_tree resync) + load the event's mesh — the same aicm-path fast
// path the GUI's own edits use (no doc reopen, no handoff file).
void applyChangedActive(AppState& st, const std::string& docId, const json& d) {
    const int idx = tabIndexForDoc(st, docId);
    if (idx != st.activeTab) return;
    AppState::DocTab& t = st.docTabs[idx];
    const bool mirror = t.aiHandoff;
    if (mirror) st.suppressTabDirty = true;  // a mirror refresh is not a user edit
    st.docMirror.resync(st.worker);          // revision/tree/owner_map + listener
    if (mirror) st.suppressTabDirty = false;
    const std::string aicm = d.value("aicm_path", std::string());
    if (!aicm.empty()) {
        std::string le;
        if (!st.loadDocMesh(aicm, le) && !le.empty())
            st.addLog("[daemon] loadDocMesh (doc.changed): " + le);
    }
    if (DocSession* s = st.activeSession()) {  // keep the fast-switch cache honest
        s->cachedAicmPath = aicm;
        s->cachedRevision = st.docMirror.revision();
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

void handleEvent(AppState& st, const json& evt) {
    const std::string name = evt.value("event", std::string());
    const json& d = evt.contains("data") && evt["data"].is_object() ? evt["data"]
                                                                    : json::object();
    const std::string docId = d.value("doc_id", std::string());

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
        if (idx != st.activeTab) {
            st.docTabs[idx].aiPulse = true;  // background: glow; switch-in refreshes
            return;
        }
        if (userMidEdit(st) || st.docTabs[idx].dirty) {
            pendingChanged[docId] = d;  // defer, retry when safe (poller semantics)
            st.docTabs[idx].aiPulse = true;
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
        if (!st.docTabs[idx].aiHandoff) {
            st.addLog("[daemon] doc.closed for user tab " + docId + " (reason " + reason + ")");
            return;  // never auto-close a tab the user opened themselves
        }
        if (reason == "last_writer_detached") {
            st.docTabs[idx].aiPulse = false;  // keep the final view on screen
            return;
        }
        st.closeDocTab(idx);
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

bool tryBootstrap(AppState& st, aicad_ipc::EngineClient& c) {
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
        for (const auto& doc : dl["docs"]) {
            const std::string id =
                doc.is_object() ? doc.value("doc_id", std::string()) : std::string();
            if (!id.empty()) openMirrorTab(st, id);
        }
    }
    return true;
}

}  // namespace

void engineEventsTick(AppState& st) {
    if (!engine::daemonMode()) return;
    auto* c = engine::client();
    if (!c) return;

    if (!bootstrapped) bootstrapped = tryBootstrap(st, *c);

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
        if (it != pendingChanged.end() && !st.docTabs[st.activeTab].dirty) {
            const json d = it->second;
            applyChangedActive(st, activeId, d);
        }
    }
}

}  // namespace core
