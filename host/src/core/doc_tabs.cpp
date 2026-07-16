// Document tabs (multi-document) — the per-tab operations on AppState.
//
// The host stays single-document at the live DocMirror level, while every DocSession parks an
// immutable mirror snapshot, parsed tessellation, and renderer GPU buffers keyed by doc id+revision.
// Switching an unchanged already-open tab restores/binds those caches without a worker or disk
// round-trip. Changed/cold tabs clear stale geometry immediately and rebuild asynchronously.
//
// Per-document PropertyManager, sketch, evaluate, selection, visibility, and camera state are parked
// and restored with the same switch so an in-progress edit resumes in the correct document.
#include <chrono>
#include <cstdio>
#include <set>
#include <string>

#include "app_state.h"

#include "../worker_client.h"
#include "../renderer.h"
#include "../render/aicm_loader.h"    // complete DocMesh for per-session parsed tess cache
#include "engine_backend.h"           // Phase 4: daemon mirror tabs detach, not doc_close
#include "ipc_client.h"               // services/ipc EngineClient (daemon detach_doc)
#include "../ui/sketch_mode.h"        // per-document SketchMode owned by each DocSession
#include "../ui/property_manager.h"   // per-document PropertyManager owned by each DocSession
#include "../ui/evaluate_dialogs.h"   // per-document EvaluateDialogs (Measure/Mass/Section) owned by DocSession
#include "../ui/sim_manager.h"        // per-document SimManager (simulation studies) owned by DocSession
#include "../ui/feature_tree.h"       // featureTreeResetTransient — clear tree statics on a tab switch
#include "../ui/ecad/ecad_panel.h"    // Wave 2: point the ECAD editor at the active Schematic/PCB tab
#include "../ui/ecad/ecad_mirror.h"   // Wave 2: complete EcadMirror type for setDocId on switch

// ---- DocSession (multi-document per-tab editing/display state) -------------------------------
// Defined here (not in app_state.h) so the unique_ptr members see the COMPLETE PropertyManager /
// SketchMode / EvaluateDialogs types pulled in above — app_state.h only forward-declares them.
DocSession::DocSession()
    : pm(std::make_unique<ui::PropertyManager>()),
      sketch(std::make_unique<ui::SketchMode>()),
      evaluate(std::make_unique<ui::EvaluateDialogs>()),
      sim(std::make_unique<ui::SimManager>()) {}
DocSession::~DocSession() = default;

DocSession* AppState::activeSession() {
    if (activeTab < 0 || activeTab >= (int)docTabs.size()) return nullptr;
    DocTab& t = docTabs[activeTab];
    if (t.kind != DocTab::TabKind::Part && t.kind != DocTab::TabKind::Assembly) return nullptr;
    return t.session.get();
}

void AppState::ensureTabSession(DocTab& t) {
    if (t.kind != DocTab::TabKind::Part && t.kind != DocTab::TabKind::Assembly) return;
    if (!t.session) t.session = std::make_shared<DocSession>();
}

void AppState::reseatActiveSubsystems(DocSession* s) {
    propertyManager = s ? s->pm.get()       : fallbackPropertyManager;
    sketchMode      = s ? s->sketch.get()   : fallbackSketchMode;
    evaluateDialogs = s ? s->evaluate.get() : fallbackEvaluate;
    simManager      = s ? s->sim.get()      : fallbackSimManager;
}

// Release closed-tab sessions once nothing they own can still be touched by a pending async result.
// A DocSession's PM / sketch / evaluate capture `this` in their st.rpc completion lambdas; those run
// on the GL thread in rpc.drain(). While rpc.busy() is true a queued/undrained result may still fire
// against a retired session, so we keep it alive until the queue is fully drained. Called once per
// frame from run_gui (after the rpc drain).
void AppState::sweepRetiredSessions() {
    if (!retiredSessions.empty() && !rpc.busy()) retiredSessions.clear();
}

// Safe JSON string accessor (local; avoids pulling the app_state.h jstr overloads here).
static std::string jtab(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

// doc_ids with a "Loading <title>…" toast in flight (async switch-in rebuild pending). Re-entering
// switchToTab for the same doc while its rebuild drains must not post the toast again — dedup here,
// erased in the rebuild-completion callback.
static std::set<std::string> loadingToastInFlight;

static void logTabPerf(AppState& st, const std::string& line) {
    st.addLog(line);
    std::fprintf(stderr, "%s\n", line.c_str());
    std::fflush(stderr);
}

// The display title for a document: its tree "name", else a kind-appropriate default.
static std::string docTitle(const nlohmann::json& tree, bool isAssembly) {
    std::string n = tree.is_object() ? jtab(tree, "name") : std::string();
    if (!n.empty()) return n;
    return isAssembly ? "Assembly1" : "Part1";
}

// See app_state.h: filename-first label for a tab. Strip directory + one trailing extension so an
// opened "C:\parts\bracket.step+" reads as "bracket"; fall back to the tree-derived title otherwise.
std::string AppState::DocTab::displayTitle() const {
    // Live MCP-handoff mirror: never show its on-disk name ("live", from live.step+). Label it as
    // the AI's part using the tree-derived title so the user reads "AI: bracket", not "live".
    // Once the user has ADOPTED it (edited it), it's their doc — drop the "AI:" prefix.
    if (aiHandoff && !userAdopted)
        return "AI: " + (title.empty() ? std::string("part") : title);
    if (!path.empty()) {
        std::size_t slash = path.find_last_of("/\\");
        std::string base = (slash == std::string::npos) ? path : path.substr(slash + 1);
        std::size_t dot = base.find_last_of('.');
        if (dot != std::string::npos && dot > 0) base = base.substr(0, dot);
        if (!base.empty()) return base;
    }
    return title.empty() ? std::string("Untitled") : title;
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
    // Multi-document: park the currently-active CAD tab's edit/display state before this new doc
    // becomes active, so switching BACK to it later restores its selection/mask/readouts (its PM /
    // sketch / measure already live on its own DocSession and survive regardless). Save its camera
    // too (mirrors the switchToTab leaving-camera capture).
    // The worker mirror already points at the new document, but the renderer still owns the leaving
    // tab's immutable buffers. Park them before the first upload for the new tab can replace them.
    if (renderer) renderer->parkActiveMeshes();
    if (DocSession* prev = activeSession()) {
        DocTab& cur = docTabs[activeTab];
        cur.yaw = yaw; cur.pitch = pitch; cur.zoom = zoom;
        cur.pan[0] = pan[0]; cur.pan[1] = pan[1]; cur.pan[2] = pan[2];
        cur.camValid = true;
        captureDocState(*prev);
    }
    DocTab t;
    t.kind = isAssembly ? DocTab::TabKind::Assembly : DocTab::TabKind::Part;
    t.docId = docMirror.docId();
    t.isAssembly = isAssembly;
    t.title = docTitle(docMirror.tree(), isAssembly);
    t.dirty = false;
    // A freshly created/opened doc's live camera becomes this tab's camera the first time the user
    // switches away (camValid stays false until then, so the default iso view is used on return).
    docTabs.push_back(std::move(t));
    activeTab = (int)docTabs.size() - 1;
    // Give the new tab its own editing/display session and point the active-subsystem borrows at it.
    ensureTabSession(docTabs[activeTab]);
    reseatActiveSubsystems(docTabs[activeTab].session.get());
}

// Multi-document: park the active CAD tab's per-doc VALUE state into its DocSession before a switch.
// This REPLACES the old "cancel the in-progress edit" behavior — a switch now PRESERVES the open PM
// form / active sketch / measurement (those live on the session's own PropertyManager / SketchMode /
// EvaluateDialogs and survive untouched; only the AppState value fields are parked here). No-op on a
// non-CAD tab (Drawing / Schematic / PCB have no DocSession).
static void parkActiveCadSession(AppState& st) {
    if (DocSession* s = st.activeSession()) st.captureDocState(*s);
}

// Wave 2 (GUI rework): open an already-created worker ECAD doc as a new Schematic/PCB tab + make it
// active. The caller (the New-Document chooser / ribbon New Schematic|PCB) has already run ecad_new
// and adopted the result into the editor mirror (st.ecadDocId/ecadRevision are set). This appends a
// matching tab carrying that ecad id/revision and points the editor at it. The Drawing-tab discipline
// applies: no DocMirror touch. The leaving CAD tab's edit/display state is PARKED (not cancelled) so
// the part behind the ECAD canvas keeps its in-progress PM form / sketch / measure.
void AppState::addEcadTab(const std::string& kind, const std::string& ecadDocId_,
                          int ecadRevision_, const std::string& title) {
    if (ecadDocId_.empty()) return;
    parkActiveCadSession(*this);
    // Save the leaving 3D tab's camera (mirrors switchToTab step 2; skipped for drawing/ECAD tabs).
    if (activeTab >= 0 && activeTab < (int)docTabs.size() &&
        docTabs[activeTab].kind != DocTab::TabKind::Drawing &&
        docTabs[activeTab].kind != DocTab::TabKind::Schematic &&
        docTabs[activeTab].kind != DocTab::TabKind::PCB) {
        DocTab& cur = docTabs[activeTab];
        cur.yaw = yaw; cur.pitch = pitch; cur.zoom = zoom;
        cur.pan[0] = pan[0]; cur.pan[1] = pan[1]; cur.pan[2] = pan[2];
        cur.camValid = true;
    }

    DocTab t;
    t.kind = (kind == "pcb") ? DocTab::TabKind::PCB : DocTab::TabKind::Schematic;
    t.ecadDocId = ecadDocId_;
    t.ecadRevision = ecadRevision_;
    t.title = title.empty() ? (kind == "pcb" ? std::string("PCB1") : std::string("Schematic1"))
                            : title;
    docTabs.push_back(std::move(t));
    activeTab = (int)docTabs.size() - 1;
    // Keep the AppState reflection in step (app.cpp ECAD helpers read st.ecadDocId/ecadRevision).
    ecadDocId = ecadDocId_;
    ecadRevision = ecadRevision_;
    animActive = false;
    showRibbon = true;
    ribbonTab = RT_PCB;
    wantFocusEcad = 6;
    // An ECAD tab does not use the CAD subsystems — point st.propertyManager / sketchMode /
    // evaluateDialogs back at the fallback singletons so nothing acts on the parked part behind it.
    reseatActiveSubsystems(nullptr);
    addLog("[tabs] new ECAD " + kind + " tab '" + docTabs[activeTab].title + "' (" + ecadDocId_ + ")");
}

void AppState::switchToTab(int idx) {
    if (idx < 0 || idx >= (int)docTabs.size()) return;
    // Before leaving the current tab, note whether it's an AI mirror whose agent has FINISHED and
    // that the user never adopted or dirtied — such a tab is kept alive only while it's the one the
    // user is looking at, so switching away is its cue to auto-close (detach-only → recoverable).
    std::string leavingAutoCloseId;
    if (activeTab >= 0 && activeTab < (int)docTabs.size() && activeTab != idx) {
        const DocTab& lv = docTabs[activeTab];
        if (lv.aiHandoff && lv.agentDone && !lv.userAdopted && !lv.dirty &&
            lv.docId != docTabs[idx].docId)
            leavingAutoCloseId = lv.docId;
    }

    switchToTabCore(idx);

    if (!leavingAutoCloseId.empty()) {
        for (int i = 0; i < (int)docTabs.size(); ++i)
            if (docTabs[i].docId == leavingAutoCloseId && i != activeTab) {
                addLog("[tabs] auto-closing finished AI tab on switch-away: " + leavingAutoCloseId);
                closeDocTab(i, /*detachOnly=*/true);
                break;
            }
    }
}

void AppState::switchToTabCore(int idx) {
    if (idx < 0 || idx >= (int)docTabs.size()) return;
    const auto switchStarted = std::chrono::steady_clock::now();
    const bool cacheInvalidated = docTabs[idx].meshCacheInvalidated;
    docTabs[idx].aiPulse = false;   // the user is now looking at it — stop the AI-activity glow
    // Already-active no-op early-out (part/asm: also require the mirror to match; drawing/ECAD: by
    // index, since they don't drive the DocMirror — their id lives in a separate worker registry).
    if (idx == activeTab) {
        if (docTabs[idx].kind == DocTab::TabKind::Drawing ||
            docTabs[idx].kind == DocTab::TabKind::Schematic ||
            docTabs[idx].kind == DocTab::TabKind::PCB) return;
        if (docMirror.docId() == docTabs[idx].docId) return;
    }

    // 1) Park the LEAVING CAD tab's per-doc value state into its session (its PM form / sketch /
    //    measure live on the session and are preserved — switching no longer cancels them).
    parkActiveCadSession(*this);

    // 2) Save the LEAVING tab's camera so it keeps its own view (only meaningful for a 3D part/asm
    //    tab — a Drawing tab's "camera" is its sheet pan/zoom, and an ECAD tab's is its 2D canvas
    //    pan/zoom, both persisted live in their own state; the 3D camera doesn't apply to either).
    if (activeTab >= 0 && activeTab < (int)docTabs.size() &&
        docTabs[activeTab].kind != DocTab::TabKind::Drawing &&
        docTabs[activeTab].kind != DocTab::TabKind::Schematic &&
        docTabs[activeTab].kind != DocTab::TabKind::PCB) {
        DocTab& cur = docTabs[activeTab];
        cur.yaw = yaw; cur.pitch = pitch; cur.zoom = zoom;
        cur.pan[0] = pan[0]; cur.pan[1] = pan[1]; cur.pan[2] = pan[2];
        cur.camValid = true;
    }

    DocTab& dst0 = docTabs[idx];

    // 2b) DRAWING tab: do NOT touch the DocMirror (a drawing id is not a doc_* session). The active
    //     part/asm stays loaded behind the sheet; the central area branches to the 2D renderer on
    //     activeTabIsDrawing(). Re-fetch drawing_get on activation so source-part edits reflect.
    if (dst0.kind == DocTab::TabKind::Drawing) {
        activeTab = idx;
        activeWorkspace = Workspace::Drawing;   // the sheet renders in the Drawing workspace
        animActive = false;
        reseatActiveSubsystems(nullptr);   // drawing tab: CAD subsystems fall back to the singletons
        // A drawing has no Assembly/PCB context; otherwise it keeps whatever ribbon was forward.
        if (ribbonTab == RT_Assembly || ribbonTab == RT_PCB) ribbonTab = RT_Features;
        refreshDrawingTab(idx);
        addLog("[tabs] switched to drawing '" + dst0.title + "' (" + dst0.drawingDocId + ")");
        return;
    }

    // 2c) SCHEMATIC / PCB tab: like a Drawing, do NOT touch the DocMirror — an ECAD id lives in the
    //     worker's separate ecad_* registry, and the central area shows the ECAD editor canvas (the
    //     active part/asm stays loaded behind it). Point the editor at this tab's doc + revision and
    //     refresh its mirror (ecad_tree) so the canvas reflects any out-of-band edits, then surface
    //     the ECAD dock with the PCB ribbon forward. activeTab is set BEFORE the mirror load so the
    //     ecad_panel state() resolver targets this tab's per-document EcadEditorState.
    if (dst0.kind == DocTab::TabKind::Schematic || dst0.kind == DocTab::TabKind::PCB) {
        activeTab = idx;
        activeWorkspace = Workspace::Electronics;   // the ECAD canvas only renders on this workspace
        animActive = false;
        reseatActiveSubsystems(nullptr);  // ECAD tab: CAD subsystems fall back to the singletons
        ecadDocId   = dst0.ecadDocId;     // mirror the active ECAD doc onto AppState (app.cpp helpers)
        ecadRevision = dst0.ecadRevision;
        if (!ecadDocId.empty()) {
            ui::ecadEditorMirror().setDocId(ecadDocId);
            nlohmann::json tr;
            std::string err;
            if (worker.call("ecad_tree", {{"doc_id", ecadDocId}}, tr, err) &&
                tr.is_object() && !tr.contains("code")) {
                if (tr.contains("revision") && tr["revision"].is_number_integer())
                    ecadRevision = tr["revision"].get<int>();
                dst0.ecadRevision = ecadRevision;
                ui::ecadEditorLoadResponse(tr);
            } else if (!err.empty()) {
                addLog("[tabs] ecad_tree on switch failed for " + ecadDocId + ": " + err);
            }
        }
        showRibbon = true;
        ribbonTab = RT_PCB;
        wantFocusEcad = 6;                 // surface the ECAD editor dock (focus settles over frames)
        addLog("[tabs] switched to ECAD '" + dst0.title + "' (" + dst0.ecadDocId + ")");
        return;
    }

    DocTab& dst = docTabs[idx];
    ensureTabSession(dst);   // a never-activated tab gets its session on first switch-in

    // 3) Pure host-side bind for an unchanged already-open document. No WorkerClient call is made:
    //    its global call mutex is intentionally bypassed, so an agent job on another document cannot
    //    stall the UI. A pushed foreign revision binds only after its background prewarm completes.
    suppressTabDirty = true;
    std::string aicmPath, err;
    bool useLocalCache = false;
    const bool awaitingPushedMesh =
        cacheInvalidated && dst.session->cachedRevision >= 0 &&
        dst.session->cachedMirror.valid() &&
        dst.session->cachedMirror.docId == dst.docId &&
        dst.session->cachedMirror.revision == dst.session->cachedRevision &&
        !dst.session->cachedAicmPath.empty();
    if (cacheInvalidated && !awaitingPushedMesh) {
        if (renderer) renderer->discardCachedMeshes(dst.docId);
        dst.session->cachedAicmPath.clear();
        dst.session->cachedRevision = -1;
        dst.session->cachedMirror = {};
        dst.session->cachedDocMesh.reset();
    } else if (!cacheInvalidated) {
        useLocalCache = dst.session->cachedRevision >= 0 &&
                        dst.session->cachedMirror.valid() &&
                        dst.session->cachedMirror.docId == dst.docId &&
                        dst.session->cachedMirror.revision == dst.session->cachedRevision &&
                        dst.session->cachedDocMesh;
        if (useLocalCache) aicmPath = dst.session->cachedAicmPath;
    }
    const bool ok = useLocalCache;
    if (awaitingPushedMesh) {
        // The push arrived before its background prewarm finished. Bind its tree immediately and
        // promote the same AICM path to a visible async load — never serialize behind doc_rebuild.
        if (renderer) renderer->parkActiveMeshes();
        activeTab = idx;
        if (activeWorkspace != Workspace::Model && activeWorkspace != Workspace::Simulate &&
            activeWorkspace != Workspace::Manufacture)
            activeWorkspace = Workspace::Model;
        reseatActiveSubsystems(dst.session.get());
        restoreDocState(*dst.session);
        suppressTabDirty = true;
        docMirror.restoreSnapshot(dst.session->cachedMirror);
        dst.isAssembly = docMirror.tree().is_object() &&
                         (jtab(docMirror.tree(), "schema") == "aicad.asm/1");
        dst.title = docTitle(docMirror.tree(), dst.isAssembly);
        suppressTabDirty = false;
        ui::featureTreeResetTransient();
        zoomToBBoxRequested = false;
        unloadDocMesh();
        loadDocMeshAsync(dst.session->cachedAicmPath);
        const double shellMs = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - switchStarted).count();
        logTabPerf(*this, "[perf] tab_switch doc=" + dst.docId +
                   " path=pushed-async shell=" + std::to_string(shellMs) + "ms");
        return;
    }
    if (!ok) {
        // Async activation path: finish the tab UI shell now, apply tree/mesh when the call
        // drains. doc_activate serves the daemon's warm read-cache compile (no recompile /
        // re-tess when the doc is unchanged — the shared-daemon AI-audit case); it falls back
        // to the full forced rebuild inside the engine only when that cache is cold.
        const std::string rebuildDocId = dst.docId;
        const int rebuildTabIdx = idx;
        if (loadingToastInFlight.insert(rebuildDocId).second)   // dedup: one toast per rebuild
            toast(ui::ToastLevel::Info, "Loading " + dst.title + "…");
        ++rebuildsInFlight;
        // Select the shell immediately but never show another document's stale geometry under it.
        // Park the leaving doc's active GPU buffers first so its own next bind stays O(1).
        if (renderer) renderer->parkActiveMeshes();
        activeTab = idx;
        if (activeWorkspace != Workspace::Model && activeWorkspace != Workspace::Simulate &&
            activeWorkspace != Workspace::Manufacture)
            activeWorkspace = Workspace::Model;
        reseatActiveSubsystems(dst.session.get());
        restoreDocState(*dst.session);
        ui::featureTreeResetTransient();
        zoomToBBoxRequested = false;
        suppressTabDirty = false;
        unloadDocMesh();
        const auto rebuildQueuedAt = std::chrono::steady_clock::now();
        const double shellMs = std::chrono::duration<double, std::milli>(
            rebuildQueuedAt - switchStarted).count();
        logTabPerf(*this, "[perf] tab_switch doc=" + rebuildDocId +
                   " path=async-rebuild shell=" + std::to_string(shellMs) + "ms");
        activateTabDocAsync(rebuildDocId, rebuildTabIdx, rebuildQueuedAt);
        return;
    }

    activeTab = idx;
    // A Part/Assembly renders in the 3D viewport, which is hosted by the Model / Simulate / Manufacture
    // workspaces. Only pull to Model when the current workspace CAN'T host a 3D part (Electronics, PLM,
    // Parts Library, AI, Reverse Eng) — so switching among parts inside Simulate/Manufacture doesn't
    // yank the user back to Model.
    if (activeWorkspace != Workspace::Model && activeWorkspace != Workspace::Simulate &&
        activeWorkspace != Workspace::Manufacture)
        activeWorkspace = Workspace::Model;
    // Re-seat the active PM / sketch / evaluate borrows at THIS document's session, then restore its
    // parked MODEL state (hidden-body mask + readouts + tree highlight) BEFORE loadDocMesh so the
    // mask is preserved across the rebuild. The viewport selection is restored AFTER the mesh load
    // (loadDocMesh clears the live selection).
    reseatActiveSubsystems(dst.session.get());
    // Result colormap/legend is GLOBAL viewport state — tear it down so the leaving doc's stress
    // overlay never paints the entering doc's mesh. FEA results are transient (re-run to see them),
    // so nothing persisted is lost. The entering doc's SimManager re-pulls its studies on first draw.
    if (simManager) simManager->clearResultOverlay(*this);
    restoreDocState(*dst.session);
    // Feature-tree file-static transients (in-progress inline rename, multi-select, the panel's
    // current save path) are NOT yet per-document; clear them on every switch exactly as the old
    // clearViewportForNewDoc path did, so doc A's rename buffer / save path can't bleed onto doc B.
    // The one-shot zoom-to-bbox request is likewise dropped (it is transient, not per-doc state).
    ui::featureTreeResetTransient();
    zoomToBBoxRequested = false;

    const auto meshBindStarted = std::chrono::steady_clock::now();
    std::string bindErr;
    if (!bindCachedDocMesh(*dst.session, bindErr)) {
        suppressTabDirty = false;
        unloadDocMesh();
        addLog("[tabs] local cache bind failed for " + dst.docId + ": " + bindErr);
        toast(ui::ToastLevel::Error, "Couldn't switch document: " + bindErr);
        return;
    }
    const double meshBindMs = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - meshBindStarted).count();
    suppressTabDirty = false;

    dst.isAssembly = docMirror.tree().is_object() &&
                     (jtab(docMirror.tree(), "schema") == "aicad.asm/1");
    dst.title = docTitle(docMirror.tree(), dst.isAssembly);

    // 5b) Restore the viewport selection now that the mesh is loaded (loadDocMesh cleared it). Assign
    //     items directly so the resolved selectors survive and the observer isn't re-fired (the picks
    //     were already resolved when first made on this doc).
    selection.mutableItems() = std::move(dst.session->selItems);
    dst.session->selItems.clear();
    hover = dst.session->hover;
    pickStatus = std::move(dst.session->pickStatus);

    // 6) Restore the destination tab's camera (or keep the fresh iso default if never saved).
    animActive = false;  // cancel any in-flight tween from the leaving doc
    if (dst.camValid) {
        yaw = dst.yaw; pitch = dst.pitch; zoom = dst.zoom;
        pan[0] = dst.pan[0]; pan[1] = dst.pan[1]; pan[2] = dst.pan[2];
    }

    // 7) Contextual ribbon (Wave 2 2D): Assembly docs want the Assembly tab forward (mirrors New
    //    Assembly). A Part has no Assembly/PCB context, so coming from either (e.g. switching from a
    //    Schematic/PCB tab, or an assembly) snaps the ribbon back to Features.
    if (dst.isAssembly) { showRibbon = true; ribbonTab = RT_Assembly; }
    else if (ribbonTab == RT_Assembly || ribbonTab == RT_PCB) ribbonTab = RT_Features;

    // SimManager is per-document and retains its known studies. Do not synchronously refresh here:
    // even a read-only RPC shares the worker queue with agent jobs and used to turn a GPU-cache bind
    // into a multi-second wait. The simulation panel refreshes when it is actually drawn.
    if (!dst.isAssembly && simManager && simManager->studyCount() > 0) {
        showSimulationTools = true;
        ribbonTab = RT_Simulation;
        leftPaneTab = LP_Simulation;
    }

    addLog("[tabs] switched to '" + dst.title + "' (doc " + dst.docId +
           ", rev " + std::to_string(docMirror.revision()) + ")");
    const double totalMs = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - switchStarted).count();
    logTabPerf(*this, "[perf] tab_switch doc=" + dst.docId + " path=gpu-cache mesh=" +
                       std::to_string(meshBindMs) + "ms total=" + std::to_string(totalMs) + "ms");
}

void AppState::activateTabDocAsync(const std::string& docId, int tabIdx,
                                   std::chrono::steady_clock::time_point queuedAt) {
    // doc_activate serves the engine's warm read-cache compile (no recompile / re-tess when
    // the doc is unchanged); a stale hostd that predates the method is handled below this
    // layer — engine::call falls back to the forced doc_rebuild on "unknown method".
    rpc.call("doc_activate", nlohmann::json{{"doc_id", docId}},
             [this, docId, tabIdx, queuedAt](const core::RpcResult& r) {
                 --rebuildsInFlight;
                 const double rebuildMs = std::chrono::duration<double, std::milli>(
                     std::chrono::steady_clock::now() - queuedAt).count();
                 logTabPerf(*this, "[perf] tab_switch_rebuild doc=" + docId +
                                  " total=" + std::to_string(rebuildMs) + "ms");
                 loadingToastInFlight.erase(docId);
                 if (tabIdx < 0 || tabIdx >= (int)docTabs.size() ||
                     docTabs[tabIdx].docId != docId) {
                     addLog("[tabs] async activation for " + docId +
                            " ignored — tab closed/changed");
                     return;
                 }
                 if (activeTab != tabIdx) {
                     // User switched away — still update that tab's session cache when possible.
                     addLog("[tabs] async activation for " + docId + " completed off-screen");
                 }
                 std::string aicm, e2;
                 if (!r.ok) {
                     toast(ui::ToastLevel::Error, "Couldn't switch document: " + r.err);
                     return;
                 }
                 // Only adopt into the live mirror if this tab is still active.
                 if (activeTab == tabIdx) {
                     suppressTabDirty = true;
                     if (!docMirror.adoptActivateResponse(docId, r.result, aicm, e2)) {
                         suppressTabDirty = false;
                         toast(ui::ToastLevel::Error, "Couldn't switch document: " + e2);
                         return;
                     }
                     suppressTabDirty = false;
                     DocTab& d = docTabs[tabIdx];
                     d.isAssembly = docMirror.tree().is_object() &&
                                    (jtab(docMirror.tree(), "schema") == "aicad.asm/1");
                     d.title = docTitle(docMirror.tree(), d.isAssembly);
                     if (!aicm.empty()) {
                         std::string lerr;
                         if (!loadDocMesh(aicm, lerr) && !lerr.empty())
                             addLog("[tabs] loadDocMesh on switch: " + lerr);
                     } else {
                         unloadDocMesh();
                         renderer->setActiveMeshIdentity(docId, docMirror.revision());
                         d.session->cachedMirror = docMirror.snapshot();
                         d.session->cachedDocMesh = std::make_shared<render::DocMesh>();
                     }
                     d.session->cachedAicmPath = aicm;
                     d.session->cachedRevision = docMirror.revision();
                     d.meshCacheInvalidated = false;
                     selection.mutableItems() = std::move(d.session->selItems);
                     d.session->selItems.clear();
                     hover = d.session->hover;
                     pickStatus = std::move(d.session->pickStatus);
                     if (d.camValid) {
                         yaw = d.yaw; pitch = d.pitch; zoom = d.zoom;
                         pan[0] = d.pan[0]; pan[1] = d.pan[1]; pan[2] = d.pan[2];
                     }
                     if (d.isAssembly) { showRibbon = true; ribbonTab = RT_Assembly; }
                     else if (ribbonTab == RT_Assembly || ribbonTab == RT_PCB)
                         ribbonTab = RT_Features;
                 } else {
                     // Park mesh path on the session so the next switch can use the fast path.
                     std::string aicmPark;
                     if (r.result.contains("tess") && r.result["tess"].is_object())
                         aicmPark = r.result["tess"].value("aicm_path", std::string());
                     if (!aicmPark.empty() && docTabs[tabIdx].session) {
                         docTabs[tabIdx].session->cachedAicmPath = aicmPark;
                         if (r.result.contains("revision") &&
                             r.result["revision"].is_number_integer())
                             docTabs[tabIdx].session->cachedRevision =
                                 r.result["revision"].get<int>();
                     }
                 }
             }, 120000);
}

void AppState::closeDocTab(int idx, bool detachOnly) {
    if (idx < 0 || idx >= (int)docTabs.size()) return;
    DocTab closing = docTabs[idx];
    // Multi-document: keep the closing tab's session alive past this erase until any async RPC its
    // PM / sketch / evaluate has in flight has drained (their completion lambdas capture `this`).
    // sweepRetiredSessions() releases it once rpc is idle. Harmless for a session with no pending
    // call (cleared next frame).
    if (closing.session) retiredSessions.push_back(closing.session);

    // Tell the worker to drop the document from its registry. (Best-effort: a failure still removes
    // the host tab so the UI never wedges on a doc the user asked to close.) A Drawing tab lives in
    // the separate drawing registry, so it closes via drawing_close {doc_id: <drawing id>}.
    if (closing.kind == DocTab::TabKind::Drawing) {
        nlohmann::json res;
        std::string err;
        if (!closing.drawingDocId.empty() &&
            !worker.call("drawing_close", {{"doc_id", closing.drawingDocId}}, res, err))
            addLog("[tabs] drawing_close failed for " + closing.drawingDocId + ": " + err);
    } else if (closing.kind == DocTab::TabKind::Schematic ||
               closing.kind == DocTab::TabKind::PCB) {
        // An ECAD tab lives in the worker's separate ecad_* registry, so it closes via
        // ecad_close {doc_id: <ecad id>} (analogue of drawing_close for the drawing registry).
        nlohmann::json res;
        std::string err;
        if (!closing.ecadDocId.empty() &&
            !worker.call("ecad_close", {{"doc_id", closing.ecadDocId}}, res, err))
            addLog("[tabs] ecad_close failed for " + closing.ecadDocId + ": " + err);
        // If this was the doc the editor is pointing at, drop the AppState reflection so a stale id
        // can't drive the panel after the tab is gone (a neighbor switch re-points it if needed).
        if (ecadDocId == closing.ecadDocId) { ecadDocId.clear(); ecadRevision = -1; }
    } else if (closing.aiHandoff && engine::daemonMode()) {
        // A daemon mirror tab VIEWS a doc another client (an agent) is building in the
        // SHARED engine. Two close paths:
        //  * detachOnly (agent-finished auto-close): drop ONLY our ro view (detach_doc).
        //    The doc goes unattached and the daemon's GC/retire tier preserves it if it
        //    was dirty — so an abandoned unsaved AI part is recoverable next launch, never
        //    silently purged. This is the data-safe default for auto-close.
        //  * manual X-click (detachOnly=false): an explicit "I'm done" — doc_discard drops
        //    our view AND, if no other client still holds the doc, closes it for good so a
        //    deliberately-closed part doesn't flood back as unsaved-recovery. If the agent
        //    is still building it, doc_discard degrades to a plain detach.
        if (auto* c = engine::client()) {
            nlohmann::json res;
            std::string err;
            c->call(detachOnly ? "detach_doc" : "doc_discard",
                    {{"doc_id", closing.docId}}, res, err);
        }
    } else {
        nlohmann::json res;
        std::string err;
        if (!worker.call("doc_close", {{"doc_id", closing.docId}}, res, err))
            addLog("[tabs] doc_close failed for " + closing.docId + ": " + err);
    }

    const bool closingActive = (idx == activeTab);
    docTabs.erase(docTabs.begin() + idx);

    if (docTabs.empty()) {
        // Last tab closed → return to the empty/start state the app shows on launch. The closing
        // doc's session (still ref-held by `closing`) destructs on return; nothing to cancel since
        // its worker doc was already closed above. Point the active subsystems back at the fallbacks.
        activeTab = -1;
        reseatActiveSubsystems(nullptr);
        docMirror.reset();
        clearViewportForNewDoc();
        if (renderer) renderer->discardCachedMeshes(closing.docId);
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
        // A background tab closed: the active doc is unchanged; just fix up the active index. The
        // active session lives on the heap (shared_ptr), so erasing a different tab from the vector
        // does not invalidate the st.propertyManager/sketchMode/evaluate borrows — but re-seat
        // defensively in case the index shift moved which session activeTab resolves to.
        if (idx < activeTab) --activeTab;
        reseatActiveSubsystems(activeSession());
    }
    if (renderer) renderer->discardCachedMeshes(closing.docId);
    addLog("[tabs] closed '" + closing.title + "'");
}

void AppState::dropBlankStarterTabs(int keepIdx) {
    // Identify the keep tab by docId so the index stays valid across the erase below.
    const std::string keepId = (keepIdx >= 0 && keepIdx < (int)docTabs.size())
                                   ? docTabs[keepIdx].docId : std::string();
    for (int i = (int)docTabs.size() - 1; i >= 0; --i) {
        const DocTab& t = docTabs[i];
        if (t.docId == keepId) continue;                       // never drop the just-opened doc
        if (t.isStarter && !t.dirty && t.path.empty()) {       // untouched launch example → discard
            addLog("[tabs] discarding blank starter '" + t.displayTitle() + "' (real file opened)");
            closeDocTab(i);
        }
    }
}

void AppState::discardWorkspaceStarter(Workspace leftWs) {
    using K = DocTab::TabKind;
    auto kindMatches = [&](K k) {
        switch (leftWs) {
            case Workspace::Model:       return k == K::Part || k == K::Assembly;
            case Workspace::Drawing:     return k == K::Drawing;
            case Workspace::Electronics: return k == K::Schematic || k == K::PCB;
            default:                     return false;
        }
    };
    // Reverse scan so a close doesn't shift the indices we haven't visited yet.
    for (int i = (int)docTabs.size() - 1; i >= 0; --i) {
        if ((int)docTabs.size() <= 1) break;                 // never strand the user with zero docs
        const DocTab& t = docTabs[i];
        if (!t.isStarter || t.dirty || !t.path.empty()) continue;  // touched / saved → keep
        if (!kindMatches(t.kind)) continue;
        addLog("[tabs] discarding untouched starter '" + t.displayTitle() + "' on leaving workspace");
        closeDocTab(i);   // tears down the engine-side doc (doc_close / ecad_close / drawing_close)
    }
}

void AppState::markActiveTabDirty() {
    // Only dirty the tab that actually matches the mirror's current doc. Mid-transition (a path
    // re-pointed the mirror but the strip hasn't reconciled yet) the active index can lag the
    // mirror; checking the id avoids dirtying the wrong document's tab.
    if (activeTab >= 0 && activeTab < (int)docTabs.size() &&
        docTabs[activeTab].docId == docMirror.docId()) {
        docTabs[activeTab].dirty = true;
        // First user edit on an AI mirror tab ADOPTS it: it becomes user-owned, so it is
        // never auto-closed when the agent finishes and IS covered by the quit warning. The
        // agent keeps co-editing the same live doc (the mirror's ro attach silently upgraded
        // to rw for this edit — engine_backend.cpp — so both writers coexist under the CAS).
        if (docTabs[activeTab].aiHandoff)
            docTabs[activeTab].userAdopted = true;
    }
}

void AppState::clearActiveTabDirty() {
    if (activeTab >= 0 && activeTab < (int)docTabs.size())
        docTabs[activeTab].dirty = false;
}

void AppState::markEcadTabDirty(const std::string& ecadDocId) {
    if (suppressTabDirty || ecadDocId.empty()) return;
    for (auto& t : docTabs)
        if ((t.kind == DocTab::TabKind::Schematic || t.kind == DocTab::TabKind::PCB) &&
            t.ecadDocId == ecadDocId) {
            t.dirty = true;
            return;
        }
}

void AppState::markDrawingTabDirty(int idx) {
    if (suppressTabDirty) return;
    if (idx >= 0 && idx < (int)docTabs.size() && docTabs[idx].kind == DocTab::TabKind::Drawing)
        docTabs[idx].dirty = true;
}

void AppState::clearDrawingDirtyForSource(const std::string& sourceDocId) {
    if (sourceDocId.empty()) return;
    for (auto& t : docTabs)
        if (t.kind == DocTab::TabKind::Drawing && t.sourceDocId == sourceDocId)
            t.dirty = false;
}

void AppState::ensureActiveTab() {
    // A Drawing tab is active: the mirror still points at the drawing's SOURCE part (we never
    // re-pointed it), so the reconcile below would steal focus back to that part's tab. Leave the
    // drawing active — the central area is showing its 2D sheet.
    if (activeTabIsDrawing()) return;
    // Wave 2: same for a Schematic/PCB tab — the DocMirror points at whatever part was last active
    // behind the ECAD editor (or nothing), so reconciling would yank focus off the ECAD tab. The
    // ECAD editor owns the center; leave the ECAD tab active.
    if (activeTabIsEcad()) return;
    if (!docMirror.hasDoc()) return;          // empty start state — nothing to track
    int idx = tabIndexForActiveDoc();
    if (idx >= 0) {
        // The mirror's doc already has a tab. Make sure it's the active one (a doc-creating path
        // may have re-pointed the mirror without going through switchToTab) and keep its title in
        // step with the live tree (renames, agent-built names). Ensure the tab has a session and the
        // active-subsystem borrows resolve to it (idempotent when the active tab didn't change).
        activeTab = idx;
        ensureTabSession(docTabs[idx]);
        reseatActiveSubsystems(docTabs[idx].session.get());
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
    if (t.kind == DocTab::TabKind::Drawing) return;  // drawing keeps its own name, not the mirror's
    // Wave 2: ECAD tabs keep their own name too — their doc lives in the ecad_* registry, not the
    // DocMirror, so the DocMirror tree's name (a part's) must not overwrite the schematic/PCB title.
    if (t.kind == DocTab::TabKind::Schematic || t.kind == DocTab::TabKind::PCB) return;
    t.isAssembly = docMirror.tree().is_object() &&
                   (jtab(docMirror.tree(), "schema") == "aicad.asm/1");
    t.title = docTitle(docMirror.tree(), t.isAssembly);
}

// ---------------------------------------------------------------------------------------------
// DRAWINGS module
// ---------------------------------------------------------------------------------------------

// The standard four-view layout placed on the blank A3 sheet (positions in sheet mm; the engine
// scales + translates each projected polyline to these anchors). Matches the wave brief.
namespace {
struct DrawingViewSpec {
    const char* type;         // "base" | "projected" | "iso"
    const char* orientation;  // "front" | "top" | "right" | "iso"
    double x, y;              // sheet-mm anchor
};
const DrawingViewSpec kStdViews[] = {
    {"base",      "front", 105.0, 180.0},
    {"projected", "top",   105.0,  80.0},
    {"projected", "right", 230.0, 180.0},
    {"iso",       "iso",   300.0,  80.0},
};
}  // namespace

void AppState::makeDrawing() {
    // Guard: need an active PART/ASM tab to project from.
    if (activeTab < 0 || activeTab >= (int)docTabs.size() ||
        docTabs[activeTab].kind == DocTab::TabKind::Drawing || !docMirror.hasDoc()) {
        toast(ui::ToastLevel::Warn, "Open a part first, then Make Drawing");
        return;
    }
    const std::string sourceId = docMirror.docId();
    const std::string sourceName = docTabs[activeTab].title;

    // 1) Create a blank drawing sheet from the source part. drawing_new returns a DRAWING id in a
    //    separate registry (NOT a doc_* id) plus the initial (view-less) drawing object.
    std::string drawingId, drawingName;
    {
        nlohmann::json res;
        std::string err;
        nlohmann::json params = {{"source_doc_id", sourceId},
                                 {"name", sourceName + " - Drawing"}};
        if (!worker.call("drawing_new", params, res, err) || !res.is_object()) {
            addLog("[draw] drawing_new failed: " + err);
            toast(ui::ToastLevel::Error, "Make Drawing failed: " + (err.empty() ? "engine error" : err));
            return;
        }
        drawingId = jstr(res, "doc_id");
        if (res.contains("drawing") && res["drawing"].is_object())
            drawingName = jstr(res["drawing"], "name");
        if (drawingId.empty()) {
            addLog("[draw] drawing_new returned no doc_id");
            toast(ui::ToastLevel::Error, "Make Drawing failed: no drawing id");
            return;
        }
    }
    if (drawingName.empty()) drawingName = sourceName + " - Drawing";

    // 2) Add the four standard views. Each is best-effort: a view that fails to project still leaves
    //    a usable drawing (the panel renders a "view failed" note for it). We keep going on errors.
    int added = 0;
    for (const auto& v : kStdViews) {
        nlohmann::json res;
        std::string err;
        nlohmann::json params = {
            {"doc_id", drawingId},
            {"type", v.type},
            {"orientation", v.orientation},
            {"position", nlohmann::json::array({v.x, v.y})},
            {"scale", 1.0},
        };
        if (worker.call("drawing_add_view", params, res, err))
            ++added;
        else
            addLog(std::string("[draw] add_view ") + v.orientation + " failed: " + err);
    }

    // 3) Fetch the assembled drawing (sheets + view geometry) and open a Drawing tab for it.
    nlohmann::json drawingDoc;
    {
        nlohmann::json res;
        std::string err;
        if (worker.call("drawing_get", {{"doc_id", drawingId}}, res, err) && res.is_object())
            drawingDoc = res;
        else
            addLog("[draw] drawing_get failed: " + err);
    }

    DocTab t;
    t.kind = DocTab::TabKind::Drawing;
    t.title = drawingName;
    t.drawingDocId = drawingId;
    t.sourceDocId = sourceId;
    t.drawingDoc = std::move(drawingDoc);
    t.drawNeedFit = true;
    // Park the source part's edit/display state before the drawing tab takes focus (it stays loaded
    // behind the sheet), then push the drawing tab and fall the CAD subsystems back to the singletons.
    parkActiveCadSession(*this);
    docTabs.push_back(std::move(t));
    activeTab = (int)docTabs.size() - 1;
    reseatActiveSubsystems(nullptr);
    if (ribbonTab == RT_Assembly) ribbonTab = RT_Features;
    addLog("[draw] created drawing '" + drawingName + "' (" + drawingId + ", " +
           std::to_string(added) + " views) from " + sourceId);
    toast(ui::ToastLevel::Info, "Drawing created with " + std::to_string(added) + " views");
}

void AppState::refreshDrawingTab(int idx) {
    if (idx < 0 || idx >= (int)docTabs.size()) return;
    DocTab& t = docTabs[idx];
    if (t.kind != DocTab::TabKind::Drawing || t.drawingDocId.empty()) return;
    nlohmann::json res;
    std::string err;
    if (worker.call("drawing_get", {{"doc_id", t.drawingDocId}}, res, err) && res.is_object()) {
        t.drawingDoc = std::move(res);
        // The source may have changed components/quantities; invalidate the cached BOM so the next
        // BOM open re-fetches. (W13: drawing_bom reads the live source assembly.)
        t.bomFetched = false;
    } else {
        addLog("[draw] refresh drawing_get failed for " + t.drawingDocId + ": " + err);
        // Keep the previously-cached drawing so the sheet doesn't blank on a transient failure.
    }
}

void AppState::adoptDrawingTab(const std::string& drawingDocId, const std::string& name,
                               const std::string& sourceDocId) {
    if (drawingDocId.empty()) return;
    // Already adopted? (a re-open of the same file should not stack duplicate drawing tabs)
    for (const auto& dt : docTabs)
        if (dt.kind == DocTab::TabKind::Drawing && dt.drawingDocId == drawingDocId) return;

    // Fetch the restored drawing's assembled sheets + view geometry (it reprojects against the
    // just-reopened part). The drawing is already registered by the engine (doc_open restore), so
    // this is exactly makeDrawing()'s step 3 without the drawing_new + add_view steps.
    nlohmann::json drawingDoc;
    {
        nlohmann::json res;
        std::string err;
        if (worker.call("drawing_get", {{"doc_id", drawingDocId}}, res, err) && res.is_object())
            drawingDoc = res;
        else
            addLog("[draw] adopt drawing_get failed for " + drawingDocId + ": " + err);
    }

    const std::string title = name.empty() ? std::string("Drawing") : name;
    DocTab t;
    t.kind = DocTab::TabKind::Drawing;
    t.title = title;
    t.drawingDocId = drawingDocId;
    t.sourceDocId = sourceDocId;
    t.drawingDoc = std::move(drawingDoc);
    t.drawNeedFit = true;
    docTabs.push_back(std::move(t));
    // Do NOT switch activeTab — the reopened part should remain the active tab; the restored
    // drawing tabs sit alongside it for the user to click into.
    addLog("[draw] restored drawing tab '" + title + "' (" + drawingDocId + ") from " +
           sourceDocId);
}
