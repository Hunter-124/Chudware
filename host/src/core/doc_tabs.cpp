// Document tabs (multi-document) — the per-tab operations on AppState.
//
// The host stays single-document at the DocMirror level: one "active" document view (tree, mesh,
// selection, caches). The worker keeps every open doc alive in its registry, so a tab switch is
// cheap: re-point DocMirror at the target doc (doc_rebuild pulls its tree + a fresh AICM mesh),
// reset the per-doc host caches, reload the mesh, and restore that tab's camera. New Part / New
// Assembly / Open append a tab + activate it (the caller already created the worker doc); closing
// the active tab activates a neighbor, and closing the last tab drops back to the empty start
// state the app shows on launch.
//
// Safety: switching/closing first cleanly exits any in-progress sketch and cancels any open
// PropertyManager form so no half-edited state leaks across documents (the "zombie state" the tree
// and forms must never carry between docs).
#include <string>

#include "app_state.h"

#include "../worker_client.h"
#include "../ui/sketch_mode.h"        // exit a mid-edit sketch before switching
#include "../ui/property_manager.h"   // cancel a mid-edit PM form before switching

// Safe JSON string accessor (local; avoids pulling the app_state.h jstr overloads here).
static std::string jtab(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

// The display title for a document: its tree "name", else a kind-appropriate default.
static std::string docTitle(const nlohmann::json& tree, bool isAssembly) {
    std::string n = tree.is_object() ? jtab(tree, "name") : std::string();
    if (!n.empty()) return n;
    return isAssembly ? "Assembly1" : "Part1";
}

int AppState::tabIndexForActiveDoc() const {
    if (!docMirror.hasDoc()) return -1;
    const std::string& id = docMirror.docId();
    for (int i = 0; i < (int)docTabs.size(); ++i)
        if (docTabs[i].docId == id) return i;
    return -1;
}

void AppState::addDocTab(bool isAssembly) {
    if (!docMirror.hasDoc()) return;
    DocTab t;
    t.docId = docMirror.docId();
    t.isAssembly = isAssembly;
    t.title = docTitle(docMirror.tree(), isAssembly);
    t.dirty = false;
    // A freshly created/opened doc's live camera becomes this tab's camera the first time the user
    // switches away (camValid stays false until then, so the default iso view is used on return).
    docTabs.push_back(std::move(t));
    activeTab = (int)docTabs.size() - 1;
}

// Cleanly leave whatever the user is editing so no state straddles a document switch/close.
static void exitActiveEdits(AppState& st) {
    if (st.sketchMode && st.sketchMode->active())
        st.sketchMode->exitSketch(st, /*keep=*/true);
    if (st.propertyManager && st.propertyManager->active())
        st.propertyManager->cancelEdits(st);
}

void AppState::switchToTab(int idx) {
    if (idx < 0 || idx >= (int)docTabs.size()) return;
    if (idx == activeTab && docMirror.docId() == docTabs[idx].docId) return;  // already active

    // 1) Leave any in-progress sketch / PM form (no zombie edit state across docs).
    exitActiveEdits(*this);

    // 2) Save the LEAVING tab's camera so it keeps its own view.
    if (activeTab >= 0 && activeTab < (int)docTabs.size()) {
        DocTab& cur = docTabs[activeTab];
        cur.yaw = yaw; cur.pitch = pitch; cur.zoom = zoom;
        cur.pan[0] = pan[0]; cur.pan[1] = pan[1]; cur.pan[2] = pan[2];
        cur.camValid = true;
    }

    DocTab& dst = docTabs[idx];

    // 3) Re-point DocMirror at the destination doc (doc_rebuild → tree + revision + owner map +
    //    fresh AICM mesh path). The worker still holds the doc, so this is a fast no-op-bump call.
    //    activate() fires the mirror listener; suppress the tab-dirty marker around it (a switch is
    //    not an edit and must not light the destination's unsaved marker).
    suppressTabDirty = true;
    std::string aicmPath, err;
    bool ok = docMirror.activate(worker, dst.docId, aicmPath, err);
    suppressTabDirty = false;

    // 4) Reset every per-doc host cache / selection / tree transient BEFORE loading the new mesh
    //    (clears the previous doc's bodies, hidden set, selection, rename buffer, pending ops...).
    clearViewportForNewDoc();

    if (!ok) {
        addLog("[tabs] switch failed: " + err);
        toast(ui::ToastLevel::Error, "Couldn't switch document: " + err);
        // The mirror may now be in an inconsistent half-state; leave activeTab as-is on the strip
        // but the doc id has likely changed — re-sync the active index to whatever the mirror holds.
        activeTab = tabIndexForActiveDoc();
        return;
    }

    activeTab = idx;
    dst.isAssembly = docMirror.tree().is_object() &&
                     (jtab(docMirror.tree(), "schema") == "aicad.asm/1");
    dst.title = docTitle(docMirror.tree(), dst.isAssembly);

    // 5) Load the destination doc's mesh (empty path / empty doc → datum-plane fallback).
    if (!aicmPath.empty()) {
        std::string lerr;
        if (!loadDocMesh(aicmPath, lerr) && !lerr.empty())
            addLog("[tabs] loadDocMesh on switch: " + lerr);
    }

    // 6) Restore the destination tab's camera (or keep the fresh iso default if never saved).
    animActive = false;  // cancel any in-flight tween from the leaving doc
    if (dst.camValid) {
        yaw = dst.yaw; pitch = dst.pitch; zoom = dst.zoom;
        pan[0] = dst.pan[0]; pan[1] = dst.pan[1]; pan[2] = dst.pan[2];
    }

    // 7) Assembly docs want the Assembly ribbon tab forward (mirrors New Assembly behavior).
    if (dst.isAssembly) { showRibbon = true; ribbonTab = 4; }
    else if (ribbonTab == 4) ribbonTab = 0;  // a part doesn't have the Assembly tab context

    addLog("[tabs] switched to '" + dst.title + "' (doc " + dst.docId +
           ", rev " + std::to_string(docMirror.revision()) + ")");
}

void AppState::closeDocTab(int idx) {
    if (idx < 0 || idx >= (int)docTabs.size()) return;
    DocTab closing = docTabs[idx];

    // Tell the worker to drop the document from its registry. (Best-effort: a failure still removes
    // the host tab so the UI never wedges on a doc the user asked to close.)
    {
        nlohmann::json res;
        std::string err;
        if (!worker.call("doc_close", {{"doc_id", closing.docId}}, res, err))
            addLog("[tabs] doc_close failed for " + closing.docId + ": " + err);
    }

    const bool closingActive = (idx == activeTab);
    docTabs.erase(docTabs.begin() + idx);

    if (docTabs.empty()) {
        // Last tab closed → return to the empty/start state the app shows on launch.
        exitActiveEdits(*this);
        activeTab = -1;
        docMirror.reset();
        clearViewportForNewDoc();
        addLog("[tabs] closed last document — empty start state");
        return;
    }

    if (closingActive) {
        // Activate a neighbor (prefer the tab now at the same index, i.e. the one that followed;
        // otherwise the new last tab). switchToTab does the full re-point + viewport refresh.
        int neighbor = idx < (int)docTabs.size() ? idx : (int)docTabs.size() - 1;
        activeTab = -1;            // force switchToTab to do a real switch (don't early-out)
        switchToTab(neighbor);
    } else {
        // A background tab closed: the active doc is unchanged; just fix up the active index.
        if (idx < activeTab) --activeTab;
    }
    addLog("[tabs] closed '" + closing.title + "'");
}

void AppState::markActiveTabDirty() {
    // Only dirty the tab that actually matches the mirror's current doc. Mid-transition (a path
    // re-pointed the mirror but the strip hasn't reconciled yet) the active index can lag the
    // mirror; checking the id avoids dirtying the wrong document's tab.
    if (activeTab >= 0 && activeTab < (int)docTabs.size() &&
        docTabs[activeTab].docId == docMirror.docId())
        docTabs[activeTab].dirty = true;
}

void AppState::clearActiveTabDirty() {
    if (activeTab >= 0 && activeTab < (int)docTabs.size())
        docTabs[activeTab].dirty = false;
}

void AppState::ensureActiveTab() {
    if (!docMirror.hasDoc()) return;          // empty start state — nothing to track
    int idx = tabIndexForActiveDoc();
    if (idx >= 0) {
        // The mirror's doc already has a tab. Make sure it's the active one (a doc-creating path
        // may have re-pointed the mirror without going through switchToTab) and keep its title in
        // step with the live tree (renames, agent-built names).
        activeTab = idx;
        syncActiveTabTitle();
        return;
    }
    // The mirror points at a document with no tab — adopt it. We can't tell part vs assembly from
    // the id alone, so read the schema from the tree.
    bool isAsm = docMirror.tree().is_object() &&
                 (docMirror.tree().value("schema", std::string()) == "aicad.asm/1");
    addDocTab(isAsm);
}

void AppState::syncActiveTabTitle() {
    if (activeTab < 0 || activeTab >= (int)docTabs.size()) return;
    DocTab& t = docTabs[activeTab];
    t.isAssembly = docMirror.tree().is_object() &&
                   (jtab(docMirror.tree(), "schema") == "aicad.asm/1");
    t.title = docTitle(docMirror.tree(), t.isAssembly);
}
