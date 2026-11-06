// Scripted screenshot/test scenarios (extracted from app.cpp's run_gui frame loop).
//
// ScenarioRunner hosts the frame-driven state machines keyed on CHUDWARE_SHOT
// (m1 / sketch / asm / fea / evalmass / activity / multidoc / appearance). tick() runs once per
// frame before
// ImGui::NewFrame() and advances whichever scenario is active; endFrame() runs after
// ImGui_ImplOpenGL3_RenderDrawData and handles the per-step backbuffer captures plus the
// scenario-driven frame-loop control (swap+continue / swap+break). Behavior-preserving
// extraction — step order, settle counts, printf transcripts, and capture paths are
// unchanged from the former inline blocks; the function-local statics became members
// (run_gui constructs one runner per call, so init-once semantics hold).
//
// CHUDWARE_SHOT=m1 writes staged captures and its saved part under
// CHUDWARE_M1_SHOT_OUT when set; otherwise it uses $CHUDWARE_REPO/scratch/m1_shots.
// CHUDWARE_SHOT=siminteractive drives real ImGui pointer/keyboard events against observed
// item hooks and a rendered face. It checks native schema refusal, pristine/dirty draft coherence,
// library material changes, mesh aliases, fatigue token save/reopen and original family gestures.
// Eight added real FEA solves compare prose/object/SID modal queries with a sampled reference,
// prove static query/compound loads through reactions and displacement, and prove nondefault
// convection ambient through resolved boundary evidence and a uniform-temperature invariant.
// Malformed compound definitions must visibly disable Run. Hydraulic zero hose length executes
// with native pump flow; zero load pressure/tolerance execute in solved handoff margins/checks.
// Three actual piping solves prove Celsius/Kelvin precedence and methane aliases through native
// properties, Reynolds numbers and mass flow. CFD null/pair semantics use actual JSON Apply
// acceptance/refusal and retained drafts (not a 3D CFD solve). A real held port drag spans a native
// same-ID agent edit/refresh and must retain its old origin and refuse stale delivery.
// Both original transient thermal schedules remain. The bounded 1200 s isolated scenario is not
// general solver certification.
// Requires CHUDWARE_ENGINE_DAEMON=0 and isolated short XDG paths so user sessions remain untouched.
// Writes scratch/siminteractive/report.json and captures; failures exit non-zero.
// CHUDWARE_SHOT=simaudit covers expanded native editors and palettes.
// Both navigate clipped setup items by hovering observed scrollable windows and sending real
// wheel events. Drags stay held while exposing offscreen destinations; cards/popovers are checked
// against annotated opened state, explicit native buttons against their actual opened label and
// fully framed JSON field. Expansion reports retain the observed controls/editor, not a presumed
// click result. Failure reports include target/clip/window/scroll evidence.
// CHUDWARE_SHOT=pipedraw draws an L, a plane-switched riser and a split tee branch
// on a fresh empty part using real projected viewport clicks and Enter/Tab events.
// It places a gate valve, submits a custom constant-K fitting through the editor's
// canonical path, solves pressure/demand boundaries and checks nodal mass balance,
// Pressure colouring, actual foreground viewport pixels, and persisted fittings.
// Quick CFD-K uses the human Source/Quality/Compute/Apply controls; missing worker
// or engine is a reported SKIP, while an available job must apply a table within 180 s.
// CHUDWARE_SHOT=hydraulics3d reuses the hydraulics circuit/sizing/simulation steps,
// checks native viewport ownership and glyph pixels, then places a component with
// real plane-click input and verifies params.layout3d in the persisted study.
// Both write scratch/<scenario>_shots/<scenario>_report.json and staged captures,
// exit non-zero on failure, and mirror the final capture to --ui-screenshot.
// CHUDWARE_SHOT=piping also checks routed-pipe pixels away from its solid obstacle.
// CHUDWARE_SHOT=demoreview opens a COPY of $CHUDWARE_SHOT_DOC (File > Open pump) under the source
// demo's folder as root, proves the copy leaves that root alone and Simulate > Piping's normal open
// lists every saved study unclipped, then captures every saved hydraulic / piping / vessel / duct
// study the way its Simulation-tree row opens it, auditing each capture; report + captures in
// scratch/demoreview_shots.
// CHUDWARE_SHOT=dwgopen opens a COPY of $CHUDWARE_SHOT_DOC (any CAD file, e.g. a .dwg) through the
// File > Open pump, records the open's warnings/toasts and the world bodies (names + volumes), then
// captures the fitted iso viewport + FeatureManager and the tree with every collapsed row expanded;
// fails by name with no bodies or fewer than two solid bodies. Captures + dwgopen_report.json land
// beside the --ui-screenshot path.
// CHUDWARE_SHOT=projectchat reloads a seeded PRIVATE session store under a different root and checks
// that the active chat and Sessions follow the selected root (startup, A/B/empty-root switches with a
// parked run, outside-root document open/tab selection, explicit other-project pick) with no
// cross-project active_by_project entry; report + captures in scratch/projectchat_shots.
// CHUDWARE_SHOT=agentlock writes through a second MCP connection during a human
// sketch and verifies human edits and undo/redo while an in-app run is active.
// It also exercises real PM cancellation with competing writes, successful and rejected delayed
// daemon holds, retained new-form drafts and actual release of both generations' leases.
// It then uses the real Simulation ribbon to create a Static study asynchronously, checking the
// active origin, selected ID against the native list, and the rendered study setup.
// JSON report and captures land beside --ui-screenshot when it has a parent directory;
// otherwise they use $CHUDWARE_REPO/scratch/agentlock_shots. Use an isolated daemon.
// CHUDWARE_SHOT=asm_refresh creates a real saved source and assembly, then edits the
// source without saving and checks active/background renderer meshes, tab selection,
// and deferred updates through a parked editor. It works with either backend; use
// private HOME/XDG/session paths and a private daemon socket when daemon mode is on.
// CHUDWARE_ASM_REFRESH_OUT selects the report/capture directory; failures exit non-zero.
// CHUDWARE_SHOT=featurestate creates a canonical numeric 40/30/20 box, saves/reopens its build
// cache, and checks real PM labels/readouts in MMGS/IPS, unchanged resync/activation/snapshot
// cleanliness, actual drag-preview dirtiness, out-of-band native edits followed by local edits,
// undo/redo preservation, saved wrapped dimensions and expression hydration.
// Use private HOME/XDG/CHUDWARE_SESSIONS_DIR paths, a unique CHUDWARE_INSTANCE_KEY and
// CHUDWARE_ENGINE_DAEMON=1: the dirty-state checks require the daemon's doc_list registry.
// CHUDWARE_FEATURESTATE_OUT selects the private output root (default scratch/featurestate_shots).
// Captures and report.json use that root; failures exit non-zero. No provider is involved.
// CHUDWARE_SHOT=model_history creates an unsaved native box and unused datum, inspects the
// rendered warning/preview and feature selection, and rejects stale cleanup after a foreign
// box-size edit. Staged composited captures require current revision geometry, including the
// newly extended area, then real controls and Ctrl+Z/redo exercise cleanup and refinement.
// A separate native open surface wall verifies Refine is disabled and cannot mutate.
// Use private HOME/APPDATA/XDG/session paths and CHUDWARE_ENGINE_DAEMON=0.
// CHUDWARE_MODEL_HISTORY_OUT selects report.json and staged captures (default scratch/model_history_shots).
// It exits non-zero on an observed-state or screenshot failure; no user part is opened or saved.
// CHUDWARE_SHOT=uxquality uses real DocMirror rebuilds and rendered ImGui pointer/keyboard
// events for fillet corrections, PM acceptance/rejection, units, datums, conditional controls,
// Hole/Thread and command availability across document/workspace modes. Typed dimensions are
// checked in the active ImGui buffer before OK and again against authoritative rebuilt state.
// Pending-result and parked-document cases preserve newer invalid edits across real FIFO callbacks;
// rendered PM status, native tree/geometry, selection and cached mesh remain independent oracles.
// It gestures through the Model, 2D and 3D sketch S-bars, disabled ribbon/search controls,
// split/overflow availability and Escape ownership over live forms, and an identity-matched
// hydrated assembly snapshot on a legacy Part-kind presented tab.
// The 3D cancellation cases frame their real viewport targets with wheel gestures. Study lifecycle
// checks hidden discovery and typed setup; the piping footer checks the rendered disabled reason,
// unchanged schematic and part on a refused click, and the actual network payload after save/reopen,
// including switching away while creation is in flight. Requires CHUDWARE_ENGINE_DAEMON=0 and private
// HOME/APPDATA/XDG/CHUDWARE_SESSIONS_DIR paths; no provider.
// CHUDWARE_UXQUALITY_OUT selects report.json/capture root (default scratch/uxquality_shots).
// CHU_UXQUALITY_CASE optionally runs one native regression in isolation: numeric_escape,
// units_blur, cancel_atomic, inward_draft, legacy_variable, legacy_equal_expression,
// fillet_reopen, thread_autosize, surface_through_all, presented_motion, sketch2d_cancel,
// equation_rejection, study_identity, sketch3d_cancel, sketch3d_foreign, study_lifecycle,
// feature_rejection, flatten_rejection, assembly_failed_component, copy_failed_history,
// copy_resync_race, obsolete_rejection, ecad_queued_origin, parked_mutation,
// parked_mechanical, piping_footer, hole_thread, or commands. Without it, all cases run
// through the full UX sequence.
// A named observed-state failure exits non-zero; this bounded smoke is not solver certification.
// Example for a single case (omit CHU_UXQUALITY_CASE to run the full chain). Each concurrent run
// needs a private directory and an explicitly assigned, unused Xvfb display number:
//   : "${DISPLAY_BASE:?assign an unused Xvfb display number}"
//   P=$(mktemp -d); mkdir -p "$P"/{home,data,config,cache,runtime,sessions,out}; chmod 700 "$P/runtime"
//   HOME="$P/home" APPDATA="$P/data" XDG_DATA_HOME="$P/data" XDG_CONFIG_HOME="$P/config" \
//   XDG_CACHE_HOME="$P/cache" XDG_RUNTIME_DIR="$P/runtime" CHUDWARE_SESSIONS_DIR="$P/sessions" \
//   CHUDWARE_REPO="$PWD" CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_SHOT=uxquality CHU_UXQUALITY_CASE=piping_footer \
//   CHUDWARE_UXQUALITY_OUT="$P/out" xvfb-run -n "$DISPLAY_BASE" -s "-screen 0 1600x1000x24" \
//   ./host/build-linux/bin/chudware --ui-screenshot "$P/out/final.png"
// CHUDWARE_SHOT=parts_catalog uses an already rights-aware ingested private .step+ catalog
// record with one non-centered 20x10x5 mm box named "Catalog fixture block". Set
// CHUDWARE_PARTS_SHOT_ID to its part_id, CHUDWARE_PARTS_SHOT_QUERY to a unique search query,
// and CHUDWARE_PARTS_SHOT_OUT to a private writable output directory. Use private HOME,
// APPDATA/XDG and daemon socket/instance paths for both ingestion and the GUI. Search and
// insert use actual controls; the PM edits the feature, saves and reopens an instance and
// checks viewport pixels. Captures and report.json land in the output directory.
// CHUDWARE_SHOT=chat_inline exercises real Ctrl+V/selection/caret input, ordered image payloads,
// reference deletion/reorder, draft/history restore, edit/cancel, and full-size viewer clicks.
// Requires xclip plus portrait.png/landscape.png in scratch/chat_inline_shots. Use isolated
// HOME/XDG paths, CHUDWARE_ENGINE_DAEMON=0, and an isolated XDG_RUNTIME_DIR/invalid
// WAYLAND_DISPLAY under Xvfb so wl-paste cannot read the user's desktop clipboard.
// Captures and report.json land in scratch/chat_inline_shots; failure exits non-zero.
// CHUDWARE_SHOT=copilot_ui exercises wrapped choices, Other typing, unit selection, file input,
// geometry/approval transitions, footer non-overlap/jump, short-window composer, and header actions.
// Use private HOME/XDG paths and CHUDWARE_ENGINE_DAEMON=0. Optional CHUDWARE_COPILOT_WIDTH
// (logical px), CHUDWARE_SCALE, CHUDWARE_THEME, CHUDWARE_COPILOT_UI_WORKSPACE=1 and
// CHUDWARE_COPILOT_UI_OUT customize the surface; default captures/report: scratch/copilot_ui_shots.
// Parallel runs need distinct CHUDWARE_SESSIONS_DIR and display instances; the default shot
// session store is shared by scenario name and cleared at startup.
// CHUDWARE_SHOT=copilot_showcase is the offline visual tour of the Copilot column: a synthetic finished
// turn (finished/failed tool rows, Thinking detail, answered question summary), two queued messages
// pinned under a fake live run with Cancel handing one back to the composer, Artifacts (real PNG/STEP/
// CSV/PDF/DXF files written under <out>/assets), Sessions (>=4 sessions across Today/Yesterday/Earlier,
// a "[Image #1]" title, row hover, kebab menu), Quick settings (Model sizes), a ~320 px narrow pass and,
// when CHUDWARE_THEME is unset, a light-theme pass. Every capture runs the ui audit and fails on
// conflicts/overflows in Copilot windows; failures exit non-zero. No network, no provider, no agent.
// Use private HOME/XDG/CHUDWARE_SESSIONS_DIR paths and CHUDWARE_ENGINE_DAEMON=0. Captures
// (01_transcript, 01b_transcript_top, 02_queued_stack, 03_queue_cancelled, 04_artifacts, 05_sessions,
// 06_sessions_hover, 07_sessions_menu, 08_quick_settings, 11_transcript_narrow, 14_artifacts_narrow,
// 15_sessions_narrow, 21_sessions_light, 24_artifacts_light, 22_transcript_light, final) and
// report.json land in CHUDWARE_COPILOT_SHOWCASE_OUT (default scratch/copilot_showcase_shots). With
// CHUDWARE_THEME set the light pass is skipped (the env fixes the theme): run once per theme value.
// Leave CHUDWARE_COPILOT_WIDTH/NARROW/COLLAPSED unset: the narrow pass sizes the window itself, and
// the checks (column ~320 px, default width restored) fail if the column width is pinned. Use an
// Xvfb screen of at least 1600x1200 (the tour resizes the window to 1280x1100).
// CHUDWARE_SHOT=text_contrast aligns a shaded box edge with real selectable Copilot prose,
// compares per-pixel contrast for the prose, header labels and model metadata against same-frame
// text-free/font-coverage references, moves the camera, and checks native selection/copy. Dark
// theme also checks its composited pinned slab.
// CHUDWARE_SCALE=1/2 and CHUDWARE_THEME=light/dark are supported. Use private HOME/XDG paths
// and CHUDWARE_ENGINE_DAEMON=0. CHUDWARE_TEXT_CONTRAST_OUT chooses the capture/report root
// (default scratch/text_contrast_shots); an explicit CHUDWARE_SESSIONS_DIR must be inside it.
// Without that override the per-shot private store is used; parallel runs need distinct stores
// and displays. report.json names pixel/focus/copy failures and a failure exits non-zero.
// CHUDWARE_SHOT=settings_routes clicks Quick Settings -> Prompts and alternates Enter/click
// command-search activation across all eight AI sections, checking rendered section windows.
// Use private HOME/XDG paths and CHUDWARE_ENGINE_DAEMON=0; failures exit non-zero and write
// scratch/settings_routes_shots/report.json with observed items/windows.
// CHUDWARE_SHOT=model_settings requires APPDATA=$PWD/scratch/model_settings_private,
// CHUDWARE_SESSIONS_DIR=$APPDATA/sessions and CHUDWARE_ENGINE_DAEMON=0. It exercises model
// assignments, simulated provider sign-out with explicit synthetic catalog rows, private-config
// permission failures/retry/discard and the composer picker. See gui-architecture.md for
// isolation and privilege requirements. Reports and captures go to scratch/model_settings_shots.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace render { class SplatRenderer; }

namespace ui {

class PropertyManager;
class EvaluateDialogs;

class ScenarioRunner {
public:
    // What the frame loop must do after endFrame() (mirrors the former inline blocks'
    // bottom-of-loop control flow).
    enum class FrameAction {
        None,      // no scenario mode active — fall through to the generic shot/swap path
        Continue,  // buffers swapped; skip the rest of this loop iteration (was `continue`)
        Break,     // scenario complete; buffers swapped; leave the frame loop (was `break`)
    };

    // The runner borrows the run_gui-owned panels the scenarios drive (same references the
    // ribbon dispatcher captures); AppState carries everything else. The PropertyManager and
    // EvaluateDialogs are now PER-DOCUMENT — the scenarios resolve them from st.propertyManager /
    // st.evaluateDialogs (re-seated on the doc they create), so they are no longer passed here. The
    // SimManager is likewise per-document (st.simManager), so the FEA scenario resolves it too.
    // Ctor + dtor are BOTH out-of-line: rsRenderer is a unique_ptr to an incomplete type
    // (render::SplatRenderer), so the compiler-generated cleanup (which the inline ctor's
    // exception path also needs) must be emitted where that type is complete (scenarios.cpp).
    ScenarioRunner();
    ~ScenarioRunner();

    // Advance the active scenario's step machine (no-op when no CHUDWARE_SHOT scenario mode
    // is active). Call once per frame, before ImGui::NewFrame().
    void tick(AppState& st);

    // Per-step screenshot capture + frame-loop control for the scenario modes; call after
    // the frame was rendered. dw/dh = framebuffer size; screenshotPath = the caller's
    // --ui-screenshot target (the final step of each scenario is mirrored to it).
    FrameAction endFrame(AppState& st, int dw, int dh, const char* screenshotPath);

private:
    // ---------- C17 M1 integration scenario (CHUDWARE_SHOT=m1) ----------
    int m1Step = 0;            // which scenario step we are on
    int m1Settle = 0;          // frames waited since the step's ops ran (let mesh repaint)
    std::string m1FilletId;    // feature id of the fillet (for the edit step)
    double m1FilletMsBeforeEdit = -1.0, m1BoxMsBeforeEdit = -1.0;
    std::string m1SavePath;
    bool m1DirReady = false;
    // When non-empty, endFrame() writes the current backbuffer to this path (a per-step
    // screenshot) and clears it. Lives across frames (the step machine sets it, the
    // capture consumes it).
    std::string m1Capture;

    // ---------- C18 sketch scenario (CHUDWARE_SHOT=sketch) ----------
    int skStep = 0, skSettle = 0;
    std::string skSavePath, skFid;
    std::string skL0, skL1, skL2, skL3, skWidthCid;
    // Relation / model-reference / Fully Define / Convert steps (3+) on a mid-height sketch.
    std::string skRelFid, skRelLine, skRelPierce, skRelVertex;
    std::string skTopEdgeSel, skVertEdgeSel, skVertexSel, skTopFaceSel;
    int skTopEdgeTid = 0, skTopFaceTid = 0;
    bool skDirReady = false;
    std::string sketchCapture;  // same per-step capture mechanism, separate path slot

    // ---------- 3D sketch scenario (CHUDWARE_SHOT=sketch3d) ----------
    int s3Step = 0, s3Settle = 0;
    std::string s3SavePath, s3Fid;
    bool s3DirReady = false;
    std::string s3Capture;  // same per-step capture mechanism, separate path slot

    // ---------- C20b assembly scenario (CHUDWARE_SHOT=asm) ----------
    int asmStep = 0, asmSettle = 0;
    std::string asmSavePath, asmBasePath, asmPegPath, asmDoc, asmK2;
    double asmSolvedZ = -999.0;
    int asmInsertsSubmitted = 0, asmInsertsDone = 0, asmInsertsFailed = 0;  // async inserts
    int asmInsertWaitFrames = 0;
    bool asmDirReady = false;
    std::string asmCapture;  // same per-step capture mechanism, separate path slot

    // ---------- C19 FEA scenario (CHUDWARE_SHOT=fea) ----------
    int feaStep = 0, feaSettle = 0, feaWaitFrames = 0;

    // ---------- Disclosure-matrix scenario (CHUDWARE_SHOT=simdisc_mech|simdisc_ecad) ----------
    int sdStep = 0, sdSettle = 0;

    // ---------- external-flow scenario (CHUDWARE_SHOT=flow) ----------
    int flowStep = 0, flowSettle = 0, flowWaitFrames = 0;

    // ---------- W17 engineering-drawing scenario (CHUDWARE_SHOT=drawing) ----------
    void runDrawing(AppState& st);
    int dwgStep = 0, dwgSettle = 0;
    bool dwgDirReady = false;
    std::string dwgSavePath, dwgCapture;

    // ---------- C19 evalmass scenario (CHUDWARE_SHOT=evalmass) ----------
    int evalStep = 0, evalSettle = 0;

    // ---------- appearance scenario (CHUDWARE_SHOT=appearance) ----------
    // Model a part, open the Settings modal on the Appearance tab (glass knobs + live glass
    // preview over the viewport), capture. Self-contained: the runner reads CHUDWARE_SHOT itself
    // (no AppState want-flag), seeded once on the first tick (apInit).
    void runAppearance(AppState& st);
    bool apInit = false, apWant = false, apDone = false;
    int apStep = 0, apSettle = 0;
    std::string apSavePath, apCapture;
    bool apDirReady = false;

    // ---------- first-run onboarding scenario (CHUDWARE_SHOT=onboarding) ----------
    // Forces each wizard step open explicitly, independent of ambient completed_version, and
    // captures all three. Optional CHUDWARE_ONBOARDING_VERIFY_MERGE=1 persists completion through
    // the production merge writer after the captures (run only with an isolated XDG_DATA_HOME).
    void runOnboarding(AppState& st);
    bool obInit = false, obWant = false, obDone = false;
    int obPhase = 0, obSettle = 0;
    std::string obSavePath, obCapture;
    bool obDirReady = false;

    // ---------- Icon pipeline (CHUDWARE_SHOT=iconpipe) ----------
    // Recipe-driven clean-viewport icon generator: read a JSON recipe list (env CHUDWARE_ICON_RECIPES),
    // and for each recipe build a demo part (newPart + applyOps), render it through the renderer's
    // transparent thumbnail path (no chrome, no gizmo, alpha=0 background) at the recipe camera, and
    // write out_dir/<name>.png. Doubles as a smoke test that every tool's op actually runs.
    // All recipes drain in ONE tick (everything on the path is synchronous; no backbuffer capture),
    // so headless runs don't pay the per-frame present cost.
    void runIconPipe(AppState& st);
    bool ipInit = false, ipWant = false;
    int ipIndex = 0, ipPx = 128;
    std::string ipOutDir;
    std::vector<nlohmann::json> ipRecipes;

    // ---------- RE-28 3DGS splat scenario (CHUDWARE_SHOT=reveng_splat) ----------
    // Load a canned Gaussian-splat .ply fixture and draw it as camera-facing billboards into the
    // backbuffer (real render::SplatRenderer draw path), then capture. Self-contained: reads
    // CHUDWARE_SHOT itself, no doc/worker geometry needed. Proves the loader + GL draw don't crash.
    void runRevengSplat(AppState& st);
    bool rsInit = false, rsWant = false, rsDone = false;
    int rsSettle = 0;
    std::string rsSavePath, rsCapture;
    bool rsDirReady = false, rsLoaded = false;
    std::unique_ptr<render::SplatRenderer> rsRenderer;

    // ---------- multi-document scenario (CHUDWARE_SHOT=multidoc) ----------
    // Verifies the per-document DocSession refactor: open a feature-edit PM form mid-edit on Part A
    // (with a viewport selection + a hidden body), switch to Part B and measure, switch back to A —
    // A's PM form / selection / hidden-body mask are intact and B is independent. Same step-machine /
    // kSettle / per-step backbuffer-capture shape as m1; runMultidoc(st) drives it from tick().
    void runMultidoc(AppState& st);
    int mdStep = 0, mdSettle = 0;
    std::string mdSavePath;
    bool mdDirReady = false;
    int mdIdxA = -1, mdIdxB = -1;        // tab indices of Part A / Part B
    std::string mdDocA, mdDocB;          // worker doc ids of Part A / Part B
    bool mdPassPm = false, mdPassDoc = false, mdPassMask = false, mdPassSel = false;
    bool mdPassBIndep = false, mdPassBMeasure = false, mdPassBFresh = false;
    std::string mdCapture;               // same per-step capture mechanism, separate path slot
    // N-document scaling: a THIRD doc C with a DIFFERENT mid-edit (chamfer form + its own selection)
    // proves the DocSession handles "parts C etc" and survives A<->C round-trips. Mirror the A/B
    // slots above. mdPassChop[k] holds the per-hop verdict for the A<->C round-trip (4 hops).
    int mdIdxC = -1;                     // tab index of Part C
    std::string mdDocC;                  // worker doc id of Part C
    bool mdPassCFresh = false;           // C's PM was fresh (inactive) before we opened its form
    bool mdPassChop[4] = {false, false, false, false};  // A<->C hop verdicts (A,C,A,C)


    // ---------- tabperf scenario (CHUDWARE_SHOT=tabperf) ----------
    // Opens two real parts, then hammers A<->B switchToTab for >=20 switches so logTabPerf
    // ([perf] tab_switch ...) can be collected from stderr. Measurement-only; no UI assertions.
    void runTabPerf(AppState& st);
    int tpStep = 0, tpSettle = 0, tpSwitches = 0;
    int tpIdxA = -1, tpIdxB = -1;
    static constexpr int kTabPerfSwitches = 24;  // 12 A->B + 12 B->A

    // ---------- frameperf scenario (CHUDWARE_SHOT=frameperf) ----------
    // Builds a real filleted/drilled part, then measures orbit, real mouse-hover picking,
    // long Copilot transcript drawing and all eight workspace routes. CHUDWARE_FRAMESTATS
    // overrides the default CHUDWARE_REPO/scratch/frameperf/framestats.json report path.
    void runFramePerf(AppState& st);
    bool fpInit = false, fpWant = false, fpDone = false, fpFailed = false;
    int fpStep = 0, fpFrames = 0, fpIdleFrames = 0, fpHoverPicks = 0;
    double fpStarted = 0.0;
    // ---------- workspaces scenario (CHUDWARE_SHOT=workspaces) ----------
    // Cycles all 8 workspace tabs (Model, Drawing, Simulate, Electronics, Manufacture, PLM,
    // PartsLibrary, AI), capturing one screenshot per workspace. Acceptance gate for the
    // workspace routing switch. Per-step backbuffer-capture pattern matches multidoc/m1.
    void runWorkspaces(AppState& st);
    int wsStep = 0, wsSettle = 0;
    bool wsDirReady = false;
    std::string wsSavePath, wsCapture;

    // Builds a grounded 2-body assembly with a distance mate, runs a Motion study,
    // and captures t0/t1/t2 where body positions differ. Proves kinematic motion
    // playback is end-to-end live.
    void runMotion(AppState& st);
    bool mtInit = false, mtWant = false, mtDone = false;
    int  mtStep = 0, mtSettle = 0, mtStudiesBefore = 0;
    bool mtDirReady = false;
    std::string mtSavePath, mtCapture;
    std::string mtBasePath, mtArmPath;   // authored part AICM paths
    std::string mtAsmDoc;                // assembly doc id
    std::string mtK1, mtK2;               // grounded base and moving arm component ids
    bool mtRunOk = false;                // motion solve succeeded

    // ---------- topology optimization scenario (CHUDWARE_SHOT=topology) ----------
    // Self-contained (reads CHUDWARE_SHOT itself, no AppState want-flag): a real box design
    // domain -> Topology ribbon tab + "+ New Study" picker -> topology study with picked
    // fixture / force / preserve faces and an exclude keep-out box -> real coarse tet4 solve with
    // two methods -> result + comparison panels -> save, reopen a copy through the engine and
    // verify the retained runs/artifacts -> thermal and flow boundary editors on real configs.
    // Any failed prerequisite or check logs "topology: FAILED [<code>]", captures
    // topology_FAILED.png, writes topology_report.json {ok:false} and exits non-zero.
    void runTopology(AppState& st);
    void topoFail(const std::string& code, const std::string& detail);
    bool topoVerifyRun(const nlohmann::json& run, const std::string& tag, std::string& why);
    void topoWriteReport();
    bool topoInit = false, topoWant = false, topoDone = false, topoFailed = false;
    int  topoStep = 0, topoSettle = 0, topoIdleFrames = 0, topoCaptureWait = 0, topoStudiesBefore = 0;
    bool topoDirReady = false, topoProgressShot = false;
    double topoWaitStart = 0.0;            // steady-clock seconds when the current wait began
    std::string topoSavePath, topoCapture;
    std::string topoShotQueued;            // capture taken once the current settle elapses
    std::string topoStudyId, topoMethodA, topoMethodB;
    int  topoRunsBefore = 0;
    nlohmann::json topoFaces;              // role -> {tid, sel} resolved box faces
    nlohmann::json topoRunIds = nlohmann::json::array();
    nlohmann::json topoReport = nlohmann::json::object();
};

}  // namespace ui
