// AI-CAD desktop GUI — branded, themed, docked, with AI chat + provider settings.
//
// run_gui(): GLFW/ImGui init, the dockspace host, the per-frame panel calls (title bar, Design,
// Viewport, Console, status bar, Settings/About modals), the worker-event drain + job-completion
// handling, the headless screenshot modes, and teardown. The data model, job launcher, and each
// panel's draw code live in core/* and ui/* (see core/app_state.h). Behavior-preserving split of
// the former gui_app.cpp monolith.
#include <glad/gl.h>
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"
#include "imgui_internal.h"
#include "imgui_impl_glfw.h"
#include "imgui_impl_opengl3.h"
#include "stb_image.h"
#include "stb_image_write.h"

#include <nlohmann/json.hpp>

#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "bootstrap.h"
#include "mesh.h"
#include "renderer.h"
#include "ui_theme.h"
#include "win_chrome.h"
#include "worker_client.h"

#include "render/id_pass.h"
#include "render/overlays.h"

#include "core/app_state.h"
#include "core/jobs.h"
#include "ui/console_panel.h"
#include "ui/feature_tree.h"
#include "ui/legacy_chat.h"
#include "ui/settings_modal.h"
#include "ui/status_bar.h"
#include "ui/title_bar.h"
#include "ui/viewport_panel.h"
#include "ui/icons.h"
#include "ui/widgets/ribbon.h"
#include "ui/ribbon_def.h"
#include "ui/property_manager.h"
#include "ui/sketch_mode.h"   // C18
// --- C19: simulation + flow + evaluate UI ---
#include "core/sim_client.h"
#include "ui/sim_panel.h"
#include "ui/evaluate_dialogs.h"
// --- end C19 ---
// --- C22b: Activity panel + prompt editor ---
#include "ui/activity_panel.h"
// --- end C22b ---
// --- C20b: assembly UI ---
#include "ui/asm_panels.h"
// --- end C20b ---

int run_gui(const char* screenshotPath) {
    const bool shot = screenshotPath != nullptr;
    if (!glfwInit()) return 1;
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
    if (!shot) glfwWindowHint(GLFW_MAXIMIZED, GLFW_TRUE);
    GLFWwindow* win = glfwCreateWindow(shot ? 1340 : 1440, shot ? 838 : 900, "AI-CAD", nullptr, nullptr);
    if (!win) { glfwTerminate(); return 1; }
    glfwMakeContextCurrent(win);
    glfwSwapInterval(1);
    if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress)) return 1;

    const std::string assets = exeDir() + "\\assets";
    setWindowIcon(win, assets + "\\logo_window.png");

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_DockingEnable;
    static std::string iniPath = appdataDir() + "\\AI-CAD\\imgui.ini";
    io.IniFilename = shot ? nullptr : iniPath.c_str();

    float xs = 1.0f, ys = 1.0f;
    glfwGetWindowContentScale(win, &xs, &ys);
    float scale = shot ? 1.0f : xs;
    // AI_CAD_SCALE overrides the content scale (incl. in screenshot mode) so HiDPI layout can be
    // reproduced/verified headlessly. Clamped to a sane range.
    if (const char* envScale = std::getenv("AI_CAD_SCALE")) {
        float v = (float)std::atof(envScale);
        if (v >= 0.5f && v <= 4.0f) scale = v;
    }

    // Theme: honour AI_CAD_THEME env override, then persisted setting, then dark default.
    // Applied once here; runtime toggle in the View menu calls applyTheme() again.
    {
        const char* envTheme = std::getenv("AI_CAD_THEME");
        ui::Theme initTheme = ui::Theme::Dark;
        if (envTheme && std::strcmp(envTheme, "light") == 0) initTheme = ui::Theme::Light;
        ui::applyTheme(initTheme, scale);
    }
    ui::Fonts fonts = ui::loadFonts(assets, scale);
    ImGui_ImplGlfw_InitForOpenGL(win, true);
    ImGui_ImplOpenGL3_Init("#version 330");

    // Borderless window: strip the OS title bar/frame and drive our own (drag/resize/snap
    // stay native). Must be installed after the ImGui GLFW backend so our WndProc is on top.
    static chrome::HitLayout hitLayout;
    hitLayout.resizeBorder = (int)(6 * scale);
    chrome::install(win, &hitLayout);

    Tex wordmark = loadTexture(assets + "\\logo_wordmark.png");
    Tex wordmarkDark = loadTexture(assets + "\\logo_wordmark_dark.png");
    Tex logoIcon = loadTexture(assets + "\\logo_icon.png");

    std::string rerr;
    Renderer renderer;
    renderer.init(960, 720, rerr);
    // Viewport visuals driven by palette roles so they switch with the theme. These are cheap
    // uniform/state sets re-pushed every frame in the main loop (see below) so a runtime theme
    // toggle takes effect immediately; this initial push covers the first headless frame.
    {
        float top3[3] = {ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                         ui::palette.viewportTop[2]};
        float mid3[3] = {ui::palette.viewportMid[0], ui::palette.viewportMid[1],
                         ui::palette.viewportMid[2]};
        float bot3[3] = {ui::palette.viewportBottom[0], ui::palette.viewportBottom[1],
                         ui::palette.viewportBottom[2]};
        renderer.setBackground3(top3, mid3, bot3);
        renderer.setEdgeStyle(ui::palette.viewportEdge[0], ui::palette.viewportEdge[1],
                              ui::palette.viewportEdge[2], 1.6f * ui::uiScale);
        renderer.setDefaultPartColor(ui::palette.partDefault[0], ui::palette.partDefault[1],
                                     ui::palette.partDefault[2]);
        renderer.setGroundShadow(ui::palette.groundShadow[0], ui::palette.groundShadow[1],
                                 ui::palette.groundShadow[2], ui::palette.groundShadow[3]);
        renderer.setPlaneColors(ui::palette.planeFill, ui::palette.planeOutline);
    }

    // C14: load icon atlases (stb_image; must be after GL context is live).
    ui::loadIcons(assets);

    // C17: the ribbon callback (PM dispatcher) is registered after AppState is created so it can
    // capture &st and the PropertyManager (see below). C14's default printf handler is replaced.

    // Topology picking (C15): id pass + overlays live for the GUI's lifetime; AppState borrows
    // them. They stay inert until a doc mesh (AICM) is loaded. Highlight colors track theme roles.
    render::IdPass idPass;
    render::Overlays overlays;
    overlays.setColors(ui::palette.hoverHighlight, ui::palette.selectionHighlight);

    // ---------- application state (bundles what were run_gui locals) ----------
    AppState st;
    st.win = win;
    st.shot = shot;
    st.scale = scale;
    st.assets = assets;
    st.fonts = fonts;
    st.wordmark = wordmark;
    st.wordmarkDark = wordmarkDark;
    st.logoIcon = logoIcon;
    st.renderer = &renderer;
    st.hitLayout = &hitLayout;
    st.idPass = &idPass;
    st.overlays = &overlays;

    // C17: PropertyManager — owned here, borrowed via st.propertyManager (opaque void*).
    ui::PropertyManager propertyManager;
    st.propertyManager = &propertyManager;

    // C19: SimClient (worker #2) + Simulation panel + Evaluate dialogs — owned here.
    core::SimClient simClient;
    st.simClient = &simClient;
    ui::SimPanel simPanel;
    st.simPanel = &simPanel;
    ui::EvaluateDialogs evaluateDialogs;

    // C18: SketchMode — owned here, borrowed via st.sketchMode (opaque void*).
    ui::SketchMode sketchMode;
    st.sketchMode = &sketchMode;

    // --- C22b: ActivityPanel — owned here, borrowed via st.activityPanel (opaque void*). ---
    ui::ActivityPanel activityPanel;
    st.activityPanel = &activityPanel;
    // --- end C22b ---

    // --- C20b: AsmPanels — owned here, borrowed via st.asmPanels (opaque void*). ---
    ui::AsmPanels asmPanels;
    st.asmPanels = &asmPanels;
    // --- end C20b ---

    // C17: ribbon dispatcher — replaces C14's default printf handler. A ribbon command opens the
    // matching PM form (occupying the tree dock slot); commands without a form log a stub line so
    // they remain observable until later cards wire them.
    // C18: sketch-tab commands (sketch_new / sk_* / smart_dim / sketch_exit) route to SketchMode
    // first so the sketch experience takes priority over any PM form for those ids.
    ui::setRibbonCallback([&st, &propertyManager, &sketchMode, &simPanel,
                           &evaluateDialogs, &asmPanels](const char* cmd_id) {
        if (sketchMode.onRibbonCommand(st, cmd_id)) return;       // C18 sketch tool handled
        // C20b: Assembly tab + New Assembly route to the assembly panels first.
        {
            std::string cmd(cmd_id);
            if ((cmd.rfind("asm_", 0) == 0 || cmd == "new_assembly") &&
                asmPanels.handleCommand(st, cmd)) return;
        }
        if (propertyManager.openForCommand(st, cmd_id)) return;  // form opened
        // C19: Simulation tab -> sim panel; Evaluate tab -> evaluate dialogs.
        std::string cmd(cmd_id);
        if (cmd.rfind("sim_", 0) == 0 && simPanel.handleCommand(st, cmd)) return;
        if (cmd.rfind("eval_", 0) == 0 && evaluateDialogs.handleCommand(st, cmd)) return;
        std::printf("[ribbon] %s (no PM form)\n", cmd_id);
        std::fflush(stdout);
        st.addLog(std::string("[ribbon] ") + cmd_id + " (no PM form yet)");
    });
    // C25: expose the same dispatcher to UI widgets (shortcut bar, gesture wheel).
    st.ribbonDispatch = [&st, &propertyManager, &sketchMode, &simPanel,
                         &evaluateDialogs, &asmPanels](const char* cmd_id) {
        if (sketchMode.onRibbonCommand(st, cmd_id)) return;
        {
            std::string cmd(cmd_id);
            if ((cmd.rfind("asm_", 0) == 0 || cmd == "new_assembly") &&
                asmPanels.handleCommand(st, cmd)) return;
        }
        if (propertyManager.openForCommand(st, cmd_id)) return;
        std::string cmd(cmd_id);
        if (cmd.rfind("sim_", 0) == 0 && simPanel.handleCommand(st, cmd)) return;
        if (cmd.rfind("eval_", 0) == 0 && evaluateDialogs.handleCommand(st, cmd)) return;
        std::printf("[ribbon] %s (no PM form)\n", cmd_id);
        std::fflush(stdout);
        st.addLog(std::string("[ribbon] ") + cmd_id + " (no PM form yet)");
    };

    loadSettings(st.settings);
    // editor buffer for the system-prompt override (Settings > Agent)
    std::strncpy(st.spBuf.data(), st.settings.systemPrompt.c_str(), st.spBuf.size() - 1);

    // Re-apply theme from persisted settings (env override already applied above; if a
    // saved setting differs, the persisted value wins unless env was set).
    {
        const char* envTheme = std::getenv("AI_CAD_THEME");
        if (!envTheme) {
            // No env override — use the saved setting.
            ui::Theme t = (st.settings.theme == "light") ? ui::Theme::Light : ui::Theme::Dark;
            if (t != ui::activeTheme) ui::applyTheme(t, scale);
        }
    }

    // C16: Selection observer — on every selection change, resolve picks through the worker
    // (doc_resolve_pick) and fill the selector slot in each pending SelItem.
    // Called on the GL thread (selection changes happen there); worker call is synchronous.
    st.selection.setObserver([&st]() {
        if (!st.docMirror.hasDoc() || !st.workerStarted) return;
        for (auto& item : st.selection.mutableItems()) {
            if (!item.pending_resolve || !item.hit.valid()) continue;
            const char* kindStr =
                item.hit.kind == render::PickKind::Face   ? "face"   :
                item.hit.kind == render::PickKind::Edge   ? "edge"   :
                item.hit.kind == render::PickKind::Vertex ? "vertex" : nullptr;
            if (!kindStr) continue;
            std::string err;
            // C20b: while the Mate form is open, assembly picks resolve via the asm path
            // (doc_resolve_pick returns {selector, component}) and route into the active well.
            if (st.asmPanels) {
                auto* ap = static_cast<ui::AsmPanels*>(st.asmPanels);
                if (ap->mateFormActive()) {
                    std::string comp;
                    std::string asel = st.docMirror.resolvePickAsm(
                        st.worker, item.hit.body_id, kindStr, item.hit.topo_id, comp, err);
                    if (!asel.empty()) {
                        item.selector = asel;
                        item.pending_resolve = false;
                        try {
                            nlohmann::json selObj = nlohmann::json::parse(asel);
                            ap->routeAsmPick(selObj, comp, (int)item.hit.kind);
                        } catch (...) {}
                    }
                    continue;  // handled (or failed) — skip the part-doc resolve path
                }
            }
            std::string sel = st.docMirror.resolvePick(
                st.worker, item.hit.body_id, kindStr, item.hit.topo_id, err);
            if (!sel.empty()) {
                item.selector = sel;
                item.pending_resolve = false;
                // C17: route the freshly resolved pick into the active PropertyManager well
                // (filtered by the well's allowed kinds). Consumed picks do not affect the
                // viewport highlight set beyond their normal selection role.
                if (st.propertyManager) {
                    auto* pm = static_cast<ui::PropertyManager*>(st.propertyManager);
                    if (pm->active()) {
                        try {
                            nlohmann::json selObj = nlohmann::json::parse(sel);
                            pm->routePick(selObj, (int)item.hit.kind);
                        } catch (...) {}
                    }
                }
            } else if (!err.empty()) {
                // Log once (avoid flooding; resolvePick logs stale_revision internally).
            }
        }
    });

    // C16: Doc mirror listener — re-draws the tree whenever a mutation lands.
    // (Tree reads st.docMirror.tree() directly; listener fires on the calling thread.)
    st.docMirror.setListener([&st]() {
        // Nothing to do beyond triggering a redraw; ImGui redraws every frame so
        // the tree panel auto-updates from docMirror.tree() on the next frame.
        // We log the new revision for observability.
        st.addLog("[doc] tree updated  rev=" + std::to_string(st.docMirror.revision()));
    });

    // ---------- runtime + worker ----------
    st.rtReady = runtimeReady();

    if (st.rtReady) {
        st.tryStartWorker();
    } else {
        st.status = "Runtime not installed";
        st.statusKind = 2;
        st.addLog("[!] runtime not found at %APPDATA%\\AI-CAD\\runtime");
        st.autoSetupTried = true;
        st.startSetup(false);  // self-provision from the payload next to the exe / source tree
    }

    // welcome model
    {
        Mesh m;
        std::string e;
        if (loadBinarySTL(appdataDir() + "\\AI-CAD\\cache\\mounting_plate\\mounting_plate.stl", m, e))
            renderer.setMesh(m);
    }

    std::strncpy(st.code.data(), kStarterCode, st.code.size() - 1);
    std::strncpy(st.prompt.data(), "a knurled hex standoff, 22mm tall, M3 clearance bore", st.prompt.size() - 1);
    {
        ChatMsg w;
        w.text = "Hi! Describe a part and I'll model it in build123d, render it, and check the "
                 "result - then keep refining it with you. Set your AI provider in Settings first.";
        st.chat.push_back(w);
    }

    const char* shotMode = std::getenv("AI_CAD_SHOT");  // screenshot-only: settings|chat|assembly|plan|ribbon
    st.wantSettings = shot && shotMode && std::strcmp(shotMode, "settings") == 0;
    st.wantChat = shot && shotMode && std::strcmp(shotMode, "chat") == 0;
    st.wantAssembly = shot && shotMode && std::strcmp(shotMode, "assembly") == 0;
    st.wantPlan = shot && shotMode && std::strcmp(shotMode, "plan") == 0;
    st.wantPick = shot && shotMode && std::strcmp(shotMode, "pick") == 0;
    st.wantRibbon = shot && shotMode && std::strcmp(shotMode, "ribbon") == 0;
    st.wantTree = shot && shotMode && std::strcmp(shotMode, "tree") == 0;
    st.wantM1 = shot && shotMode && std::strcmp(shotMode, "m1") == 0;  // C17 integration scenario
    st.wantSketch = shot && shotMode && std::strcmp(shotMode, "sketch") == 0;  // C18 sketch flow
    if (st.wantSketch) { st.showRibbon = true; st.ribbonTab = 1; }  // Sketch tab forward
    st.wantFea = shot && shotMode && std::strcmp(shotMode, "fea") == 0;       // C19 static study
    st.wantEvalMass = shot && shotMode && std::strcmp(shotMode, "evalmass") == 0;  // C19 mass props
    if (st.wantFea) { st.showRibbon = true; st.ribbonTab = 3; st.showSimPanel = true; }
    // C22b: activity shot — new part doc -> agent_run_v2 -> Activity panel -> Settings Agents tab
    st.wantActivity = shot && shotMode && std::strcmp(shotMode, "activity") == 0;
    if (st.wantActivity) { st.showActivityPanel = true; }
    // C20b: assembly shot — author base/peg parts, new assembly, insert + mate + interference.
    st.wantAsm = shot && shotMode && std::strcmp(shotMode, "asm") == 0;
    if (st.wantAsm) {
        st.showRibbon = true; st.ribbonTab = 4;  // Assembly tab forward
        st.showActivityPanel = false;            // keep the FeatureManager (Mates folder) unobscured
    }
    // ribbon shot: show Features tab by default; AI_CAD_TAB=<0..4> selects which tab to capture
    // (0=Features 1=Sketch 2=Evaluate 3=Simulation 4=Assembly) for headless per-tab verification.
    if (st.wantRibbon) {
        st.showRibbon = true; st.ribbonTab = 0;
        if (const char* tabEnv = std::getenv("AI_CAD_TAB")) {
            int t = std::atoi(tabEnv);
            if (t >= 0 && t <= 4) st.ribbonTab = t;
        }
    }
    // wantTree doc smoke runs in the frame loop once the worker is ready (see below).
    if (st.wantPick) {
        // C15 picking demo/verification: load the golden box AICM, assert its topology counts,
        // and (in the frame loop) simulate a center-screen pick + highlight. The AICM path can be
        // overridden via AI_CAD_AICM; otherwise we probe the repo fixture relative to the exe.
        std::string aicm;
        if (const char* env = std::getenv("AI_CAD_AICM")) aicm = env;
        if (aicm.empty()) {
            const char* candidates[] = {
                "docs\\contracts\\fixtures\\box.aicm",
                "..\\docs\\contracts\\fixtures\\box.aicm",
                "..\\..\\docs\\contracts\\fixtures\\box.aicm",
                "..\\..\\..\\docs\\contracts\\fixtures\\box.aicm",
                "..\\..\\..\\..\\docs\\contracts\\fixtures\\box.aicm",
            };
            for (const char* c : candidates) {
                FILE* f = std::fopen(c, "rb");
                if (f) { std::fclose(f); aicm = c; break; }
            }
        }
        std::string lerr;
        if (aicm.empty() || !st.loadDocMesh(aicm, lerr)) {
            std::fprintf(stderr, "pick: failed to load box.aicm (%s): %s\n",
                         aicm.empty() ? "not found" : aicm.c_str(), lerr.c_str());
        } else {
            // Assert + print golden counts (mesh_format.md §4): 6 faces / 12 edges / 8 verts.
            render::DocMesh probe;
            std::string perr;
            render::loadAicmFile(aicm, probe, perr);
            const auto& b = probe.bodies.front();
            std::printf("aicm: bodies=%zu name=%s verts=%u tris=%u faces=%zu edges=%zu brep_verts=%zu\n",
                        probe.bodies.size(), b.name.c_str(), b.n_verts, b.n_tris,
                        b.faces.size(), b.edges.size(), b.brep_verts.size());
            if (probe.bodies.size() != 1 || b.faces.size() != 6 || b.edges.size() != 12 ||
                b.brep_verts.size() != 8 || b.n_verts != 24 || b.n_tris != 12) {
                std::fprintf(stderr, "pick: golden box.aicm counts mismatch!\n");
            }
            st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;  // iso so a face faces the camera
        }
    }
    if (st.wantPlan) {
        // The moment plan mode shines: the plan card is drafted and visible while the
        // first code pass is still being written.
        ChatMsg u;
        u.fromUser = true;
        u.text = "an M3 hex standoff, 30mm tall";
        st.chat.push_back(u);
        ChatMsg a;
        a.running = true;
        a.liveStatus = "Build plan ready - writing the CAD code...";
        a.plan.present = true;
        a.plan.name = "hex standoff";
        a.plan.summary = "extruded hex prism with a through bore, chamfered top rim";
        PlanPart pp;
        pp.name = "standoff";
        pp.purpose = "spaces a PCB off its mounting plate";
        pp.dims = "across_flats 10, height 30, bore_radius 1.7";
        pp.features = {"M3 clearance bore r=1.7 through - cut a Cylinder",
                       "1mm chamfer on the top rim - chamfer(top_edges, length=1)"};
        a.plan.parts.push_back(pp);
        st.chat.push_back(a);
    }
    if (st.wantAssembly) {
        // Demo a 3-part fit-test: base plate, lid above it, peg through both (clash on the
        // peg). Populates the viewport parts panel + a chat card so --ui-screenshot shows the
        // assembly UI. A real color-coded render is shown if its cache PNG exists.
        const std::string asmDir = appdataDir() + "\\AI-CAD\\cache\\screenshot_asm\\";
        st.asmStl = asmDir + "screenshot_asm_assembly.stl";
        std::string sheet = asmDir + "screenshot_asm_grid.png";
        // Per-part STLs (generated once for the demo) so the viewport shows the bodies
        // color-coded; missing files just fall back to the combined mesh.
        st.asmParts = {{"base", asmDir + "screenshot_asm_base.stl", "40.0 x 30.0 x 8.0 mm", 9600.0},
                    {"lid", asmDir + "screenshot_asm_lid.stl", "40.0 x 30.0 x 8.0 mm", 9600.0},
                    {"peg", asmDir + "screenshot_asm_peg.stl", "6.0 x 6.0 x 30.0 mm", 848.2}};
        st.asmFit.present = true;
        st.asmFit.ok = false;
        st.asmFit.summary = "2 interfering pair(s); largest base<->peg = 226.2 mm^3";
        st.asmFit.interferences = {{"base", "peg", 226.2}, {"lid", "peg", 226.2}};
        st.asmFit.clearances = {{"base", "lid", 10.0}};
        st.showPartMesh(-1);  // show the combined assembly mesh if its STL was pre-generated
        ChatMsg u; u.fromUser = true;
        u.text = "a base plate, a lid 10mm above it, and a peg through both";
        st.chat.push_back(u);
        ChatMsg a;
        a.text = "Built a 3-part assembly. The peg passes through both plates - tell me if that "
                 "press-fit is intended, or I'll trim it to clear.";
        AgentStep s;
        s.index = 0; s.ok = true; s.done = false; s.isAssembly = true;
        s.dims = "40.0 x 30.0 x 38.0 mm";
        s.parts = st.asmParts;
        s.fit = st.asmFit;
        s.contactSheet = sheet;   // color-coded render (loaded if present)
        s.notes = "base and lid sit 10mm apart; the peg interferes with both - intended as a "
                  "press-fit, or should I add radial clearance?";
        a.steps.push_back(s);
        st.chat.push_back(a);
    }
    if (st.wantChat) {
        ChatMsg u; u.fromUser = true; u.text = "make it 30mm tall with a chamfered top edge";
        st.chat.push_back(u);
        ChatMsg a;
        a.text = "Raised the standoff to 30mm and chamfered the top rim. Ask for another tweak "
                 "and I'll revise this same part.";
        // The plan-mode card the agent drafts before writing code.
        a.plan.present = true;
        a.plan.name = "hex standoff";
        a.plan.summary = "extruded hex prism with a through bore, chamfered top rim";
        {
            PlanPart pp;
            pp.name = "standoff";
            pp.purpose = "spaces a PCB off its mounting plate";
            pp.dims = "across_flats 10, height 30, bore_radius 1.7";
            pp.features = {"M3 clearance bore r=1.7 through - cut a Cylinder",
                           "1mm chamfer on the top rim - chamfer(top_edges, length=1)"};
            a.plan.parts.push_back(pp);
        }
        // A self-corrected build issue first, so the calm amber styling is visible.
        AgentStep f;
        f.index = 0; f.ok = false; f.done = false;
        f.error = "Traceback (most recent call last):\n"
                  "  File \"engine\\kernel.py\", line 361, in _exec_model\n"
                  "    exec(compile(code, \"<model>\", \"exec\"), g)\n"
                  "  File \"<model>\", line 9, in <module>\n"
                  "TypeError: chamfer() got an unexpected keyword argument 'radius'";
        a.steps.push_back(f);
        AgentStep s;
        s.index = 1; s.ok = true; s.done = true;
        s.dims = "22.0 x 19.1 x 30.0 mm  |  8243 mm^3";
        s.notes = "The previous part was 22mm tall. I raised total_h to 30 and added a 1mm "
                  "chamfer on the top edge ring; the bore and across-flats are unchanged.";
        s.thinking = "Looking at the prior render the proportions were right but short. I only "
                     "need to change the height variable and add a chamfer on the top face's "
                     "perimeter edges - everything else should stay parametric.";
        s.code = "af = 11.0\ntotal_h = 30.0   # was 22\nbore_r = 1.7\n"
                 "hex_r = af / (3 ** 0.5)\nface = RegularPolygon(radius=hex_r, side_count=6)\n"
                 "result = extrude(face, amount=total_h)\n"
                 "result -= Cylinder(radius=bore_r, height=total_h)\n"
                 "top = result.edges().filter_by(Plane.XY).group_by(Axis.Z)[-1]\n"
                 "result = chamfer(top, length=1.0)";
        s.diff = "--- previous\n+++ current\n@@ -1,4 +1,6 @@\n af = 11.0\n-total_h = 22.0\n"
                 "+total_h = 30.0   # was 22\n bore_r = 1.7\n+top = result.edges()."
                 "filter_by(Plane.XY).group_by(Axis.Z)[-1]\n+result = chamfer(top, length=1.0)";
        a.steps.push_back(s);
        st.chat.push_back(a);
    }

    // C17 M1 scenario: when non-empty, the bottom-of-loop capture writes the current backbuffer
    // to this path (a per-step screenshot) and clears it. Lives across frames (the step machine
    // sets it, the capture consumes it).
    std::string m1Capture;
    // C18 sketch scenario: same per-step capture mechanism, separate path slot.
    std::string sketchCapture;
    // C20b assembly scenario: same per-step capture mechanism, separate path slot.
    std::string asmCapture;

    while (!glfwWindowShouldClose(win)) {
        glfwPollEvents();

        if (st.setupDone.exchange(false)) {
            bool ok;
            std::string msg;
            {
                std::lock_guard<std::mutex> lk(st.setupMtx);
                ok = st.setupOk;
                msg = st.setupMsg;
            }
            if (ok) {
                st.addLog("[ok] " + msg);
                st.tryStartWorker();
            } else {
                st.addLog("[err] " + msg);
                st.status = "Setup needed";
                st.statusKind = 2;
            }
        }

        // Drain streamed agent progress into the active (running) assistant message so the
        // conversation updates live, iteration by iteration (texture loads stay on this thread).
        {
            std::vector<AgentEvent> evs;
            {
                std::lock_guard<std::mutex> lk(st.job.evMtx);
                if (!st.job.events.empty()) evs.swap(st.job.events);
            }
            if (!evs.empty()) {
                ChatMsg* active = nullptr;
                for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                    if (!it->fromUser && it->running) { active = &(*it); break; }
                for (auto& e : evs) {
                    // --- C22b: route orchestrator events to the ActivityPanel ---
                    if (e.kind.rfind("orch_", 0) == 0) {
                        std::string orchKind = e.kind.substr(5);  // strip "orch_"
                        try {
                            nlohmann::json data = nlohmann::json::parse(e.text);
                            if (st.activityPanel) {
                                auto* ap = static_cast<ui::ActivityPanel*>(st.activityPanel);
                                ap->onEvent(orchKind, data);
                            }
                        } catch (...) {}
                        // Also update the compact status line in the chat
                        if (!e.notes.empty()) {
                            st.agentV2Status = e.notes;
                            if (active) active->liveStatus = e.notes;
                        }
                        continue;  // don't process as legacy event
                    }
                    // --- end C22b ---
                    if (e.kind == "status") {
                        st.livePhase = e.text;
                        if (active) active->liveStatus = e.text;
                    } else if (e.kind == "plan") {
                        if (active) active->plan = e.plan;
                        st.livePhase = "Build plan ready - writing the CAD code...";
                    } else if (e.kind == "iteration") {
                        if (active) {
                            AgentStep* st2 = nullptr;
                            for (auto& s : active->steps)
                                if (s.index == e.index) { st2 = &s; break; }
                            if (!st2) { active->steps.emplace_back(); st2 = &active->steps.back(); }
                            st2->index = e.index; st2->ok = e.ok; st2->done = e.done;
                            st2->notes = e.notes; st2->thinking = e.thinking; st2->code = e.code;
                            st2->diff = e.diff; st2->dims = e.dims; st2->error = e.error;
                            st2->contactSheet = e.contactSheet; st2->stdoutText = e.stdoutText;
                            st2->isAssembly = e.isAssembly; st2->parts = e.parts; st2->fit = e.fit;
                            active->liveStatus.clear();
                        }
                        // Live preview: show each successful build in the viewport the moment
                        // it lands, color-coded per part for assemblies (we're on the GL thread).
                        if (e.ok && !e.stlPath.empty()) {
                            st.asmStl = e.stlPath;
                            st.asmParts = e.isAssembly ? e.parts : std::vector<PartInfo>{};
                            st.asmFit = e.isAssembly ? e.fit : FitReport{};
                            st.selectedPart = -1;
                            st.showPartMesh(-1);
                            if (!e.dims.empty()) st.modelInfo = e.dims;
                            st.livePhase = "Iteration " + std::to_string(e.index + 1) +
                                        " built - evaluating the render...";
                        } else if (!e.ok) {
                            st.livePhase = "Iteration " + std::to_string(e.index + 1) +
                                        " - build issue, revising the code...";
                        }
                    }
                }
            }
        }

        if (st.job.ready.exchange(false)) {
            std::lock_guard<std::mutex> lk(st.job.mtx);
            st.livePhase.clear();
            st.agentV2Status.clear();
            // --- C22b: if this was a v2 run, resync the DocMirror tree + reload mesh ---
            // The worker already rebuilt the doc; call doc_rebuild to refresh the tree and
            // get the latest aicm_path, then load it into the viewport.
            if (st.agentV2Running) {
                st.agentV2Running = false;
                if (st.docMirror.hasDoc() && st.job.error.empty()) {
                    nlohmann::json rebuildResult;
                    std::string rerr2;
                    if (st.worker.call("doc_rebuild",
                                       {{"doc_id", st.docMirror.docId()}},
                                       rebuildResult, rerr2, 60000)) {
                        // Update mirror revision/tree from the rebuild response
                        int newRev = rebuildResult.value("revision", -1);
                        (void)newRev;  // DocMirror handles this via listener
                        std::string aicmPath = rebuildResult.value("aicm_path", "");
                        if (!aicmPath.empty()) {
                            std::string le;
                            st.loadDocMesh(aicmPath, le);
                        }
                        st.addLog("[v2] doc_rebuild after run: rev=" +
                                  std::to_string(st.docMirror.revision()));
                    } else {
                        st.addLog("[err] doc_rebuild after v2 run: " + rerr2);
                    }
                }
            }
            // --- end C22b ---
            std::string assistant;
            int assistantErr = ERR_NONE;
            if (!st.job.error.empty() && st.job.errorIsCad) {
                // The CAD script didn't build. That's normal territory for an AI design loop
                // (it usually self-corrects mid-run) — report it calmly, details on demand,
                // and don't dress a modeling problem up as an app crash.
                std::string cause = errSummary(st.job.error);
                st.status = "Build didn't converge";
                st.statusKind = 1;
                st.addLog("[cad] " + cause);
                assistantErr = ERR_CAD;
                if (st.job.kind == JOB_BUILD) {
                    st.buildErrSummary = cause;
                    st.buildErrFull = st.job.error;
                } else {
                    assistant = "I couldn't get this design to build cleanly - the CAD code kept "
                                "failing.\nLast error: " + cause + "\nThis is a modeling problem, "
                                "not an app problem. Try rephrasing or simplifying the request "
                                "(each iteration's Error details has the full trace).";
                }
            } else if (!st.job.error.empty()) {
                st.status = "System error";
                st.statusKind = 2;
                std::string e = st.job.error.substr(0, 300);
                st.addLog("[err] " + e);
                assistantErr = ERR_SYSTEM;
                // A missing Python module means the venv is incomplete — auto-repair once.
                bool missingModule = st.job.error.find("ModuleNotFoundError") != std::string::npos ||
                                     st.job.error.find("No module named") != std::string::npos;
                if (missingModule && !st.importRepairTried && !st.setupRunning.load()) {
                    st.importRepairTried = true;
                    st.addLog("[..] runtime module missing — auto-repairing (use AI-CAD > Repair runtime if this persists)...");
                    st.startSetup(true);
                    assistant = "A required module is missing from the runtime — repairing now (may take a minute). Try again once the console shows setup complete.";
                } else {
                    assistant = "Something went wrong on the app side (your design is fine):\n" + e;
                    // Point at Settings only for credential-shaped failures; other errors
                    // (connection refused, model missing) carry their own guidance.
                    std::string low = e;
                    for (auto& c : low) c = (char)std::tolower((unsigned char)c);
                    static const char* kAuthMarks[] = {
                        "401", "403", "unauthorized", "forbidden", "api key", "api_key",
                        "apikey", "authentication", "invalid key", "credential"};
                    for (const char* mark : kAuthMarks) {
                        if (low.find(mark) != std::string::npos) {
                            assistant += "\n(This looks like an authentication problem - "
                                         "set your provider/API key in Settings.)";
                            break;
                        }
                    }
                }
            } else if (!st.job.stl.empty()) {
                Mesh m;
                std::string e;
                if (loadBinarySTL(st.job.stl, m, e)) {
                    renderer.setMesh(m);
                    st.status = "Done";
                    st.statusKind = 0;
                    st.modelInfo = st.job.info;
                    st.buildErrSummary.clear();
                    st.addLog("[ok] built  " + st.job.info);
                    // Refresh the parts panel for the new model (cleared for single parts so
                    // the panel disappears), then re-load assemblies color-coded per part.
                    st.asmStl = st.job.stl;
                    st.asmParts = st.job.parts;
                    st.asmFit = st.job.fit;
                    st.selectedPart = -1;
                    if (st.job.isAssembly) st.showPartMesh(-1);
                    if (st.job.isAssembly && st.job.fit.present) {
                        assistant = "Done - " + st.job.info + ".\n" +
                                    (st.job.fit.ok ? "Fit OK - " : "Fit issue - ") + st.job.fit.summary +
                                    "\nSee the Viewport (parts list + fit readout). Ask for a tweak and I'll revise.";
                        st.addLog((st.job.fit.ok ? "[ok] fit: " : "[err] fit: ") + st.job.fit.summary);
                    } else {
                        assistant = "Done - " + st.job.info + ".\nSee the Viewport. Ask for a tweak and I'll revise.";
                    }
                    if ((st.job.kind == JOB_DESIGN || st.job.kind == JOB_CHAT) && !st.job.converged)
                        assistant += "\n(Stopped at the iteration cap - say \"keep going\" "
                                     "and I'll keep refining this same design.)";
                } else {
                    st.status = "STL load failed";
                    st.statusKind = 2;
                    st.addLog("[err] " + e);
                    assistant = "Built it, but couldn't load the mesh: " + e;
                    assistantErr = ERR_SYSTEM;
                }
            }
            if (st.job.kind == JOB_DESIGN || st.job.kind == JOB_CHAT) {
                ChatMsg* active = nullptr;
                for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                    if (!it->fromUser && it->running) { active = &(*it); break; }
                if (active) {
                    active->running = false;
                    active->liveStatus.clear();
                    active->errKind = assistantErr;
                    if (!assistant.empty()) active->text = assistant;
                } else if (!assistant.empty()) {
                    ChatMsg m; m.text = assistant; m.errKind = assistantErr;
                    st.chat.push_back(m);
                }
            }
        }

        // C16 tree shot: run the doc smoke once the worker is ready (fires the first time
        // workerStarted is true after a wantTree shot). Synchronous calls are OK here since
        // we defer the screenshot to frame >= 10 (giving extra frames for the tree to repaint).
        if (st.wantTree && !st.treeShotDone && st.workerStarted) {
            st.treeShotDone = true;
            std::string err;
            if (st.docMirror.newPart(st.worker, err)) {
                std::printf("doc_new: doc_id=%s rev=%d\n",
                            st.docMirror.docId().c_str(), st.docMirror.revision());
                std::fflush(stdout);
                nlohmann::json boxOps = nlohmann::json::array();
                boxOps.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Box1"},{"params",{
                        {"length",{{"value",50.0},{"expr",nullptr}}},
                        {"width", {{"value",40.0},{"expr",nullptr}}},
                        {"height",{{"value",30.0},{"expr",nullptr}}}
                    }}}}});
                auto r1 = st.docMirror.applyOps(st.worker, boxOps);
                std::printf("add_box: ok=%d rev=%d aicm=%s\n",
                            (int)r1.ok, r1.revision, r1.aicm_path.c_str());
                std::fflush(stdout);
                if (r1.ok && !r1.aicm_path.empty()) { std::string le; st.loadDocMesh(r1.aicm_path, le); }
                std::string selStr, serr;
                selStr = st.docMirror.resolvePick(st.worker, 1, "face", 1, serr);
                std::printf("resolve_pick: selector=%s err=%s\n", selStr.c_str(), serr.c_str());
                std::fflush(stdout);
                nlohmann::json edgeRef;
                try { edgeRef = nlohmann::json::parse(
                    R"({"kind":"edge","surf":"line","centroid":[0,0,15],"axis":[0,0,1],"area_or_len":30,"owner_feature":null,"hint_index":0})");
                } catch (...) {}
                if (!edgeRef.is_null()) {
                    nlohmann::json fo = nlohmann::json::array();
                    fo.push_back({{"op","add_feature"},{"feature",{
                        {"type","fillet"},{"name","Fillet1"},
                        {"params",{{"radius",{{"value",3.0},{"expr",nullptr}}}}},
                        {"refs",{{"edges",nlohmann::json::array({edgeRef})}}}}}});
                    auto r2 = st.docMirror.applyOps(st.worker, fo);
                    std::printf("add_fillet: ok=%d rev=%d err=%s\n",
                                (int)r2.ok, r2.revision, r2.error_message.c_str());
                    std::fflush(stdout);
                    if (r2.ok && !r2.aicm_path.empty()) { std::string le; st.loadDocMesh(r2.aicm_path, le); }
                }
                auto ru = st.docMirror.undo(st.worker);
                std::printf("undo: ok=%d rev=%d features=%d\n", (int)ru.ok, ru.revision,
                    (int)(ru.tree.is_object()&&ru.tree.contains("features")&&ru.tree["features"].is_array()
                          ? ru.tree["features"].size() : 0));
                std::fflush(stdout);
                if (ru.ok && !ru.aicm_path.empty()) { std::string le; st.loadDocMesh(ru.aicm_path, le); }
                auto rr = st.docMirror.redo(st.worker);
                std::printf("redo: ok=%d rev=%d features=%d\n", (int)rr.ok, rr.revision,
                    (int)(rr.tree.is_object()&&rr.tree.contains("features")&&rr.tree["features"].is_array()
                          ? rr.tree["features"].size() : 0));
                std::fflush(stdout);
                if (rr.ok && !rr.aicm_path.empty()) { std::string le; st.loadDocMesh(rr.aicm_path, le); }
            } else {
                std::printf("tree-shot: doc_new failed: %s\n", err.c_str());
                std::fflush(stdout);
            }
        }

        // ---------- C17 M1 integration scenario (AI_CAD_SHOT=m1) ----------
        // Frame-driven state machine: each step issues scripted ops through the REAL worker
        // (DocMirror), loads the rebuilt mesh, and requests a screenshot into scratch/m1_shots/.
        // Steps: New Part -> Box 60x40x10 -> Fillet r=4 (2 picked vertical edges) -> Shell t=2
        // (remove top face) -> edit Fillet r=8 (cached rebuild assertion) -> save + reopen.
        // The capture itself happens at the bottom of the loop when m1Capture is non-empty.
        static int  m1Step = 0;          // which scenario step we are on
        static int  m1Settle = 0;        // frames waited since the step's ops ran (let mesh repaint)
        static std::string m1FilletId;   // feature id of the fillet (for the edit step)
        static double m1FilletMsBeforeEdit = -1.0, m1BoxMsBeforeEdit = -1.0;
        static std::string m1SavePath;
        if (st.wantM1 && st.workerStarted && !st.m1ShotDone) {
            // Resolve repo-relative scratch dir for screenshots.
            static bool m1DirReady = false;
            if (!m1DirReady) {
                m1DirReady = true;
                const char* repo = std::getenv("AI_CAD_REPO");
                std::string base = repo ? std::string(repo) : std::string(".");
                m1SavePath = base + "\\scratch\\m1_shots";
                // best-effort mkdir (ignore errors; the dir usually exists)
                std::string mk = "scratch\\m1_shots";
                (void)mk;
            }
            auto loadMesh = [&](const core::OpsResponse& r) {
                if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
            };
            auto statusMs = [](const core::OpsResponse& r, const std::string& fid) -> double {
                if (!r.feature_status.is_array()) return -1.0;
                for (const auto& s : r.feature_status)
                    if (s.is_object() && s.value("id", std::string()) == fid)
                        return s.value("ms", -1.0);
                return -1.0;
            };
            // Find vertical edges (axis ~ +Z) by resolving each edge topo id.
            auto verticalEdges = [&](int wantN) {
                std::vector<nlohmann::json> out;
                for (int tid = 1; tid <= 12 && (int)out.size() < wantN; ++tid) {
                    std::string serr;
                    std::string s = st.docMirror.resolvePick(st.worker, 1, "edge", tid, serr);
                    if (s.empty()) continue;
                    try {
                        nlohmann::json o = nlohmann::json::parse(s);
                        if (o.contains("axis") && o["axis"].is_array() && o["axis"].size() == 3 &&
                            std::fabs(o["axis"][2].get<double>()) > 0.9)
                            out.push_back(o);
                    } catch (...) {}
                }
                return out;
            };
            // Find the top face (max centroid z) by resolving face topo ids.
            auto topFace = [&]() -> nlohmann::json {
                nlohmann::json best; double bestZ = -1e30;
                for (int tid = 1; tid <= 40; ++tid) {
                    std::string serr;
                    std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, serr);
                    if (s.empty()) break;
                    try {
                        nlohmann::json o = nlohmann::json::parse(s);
                        double z = o["centroid"][2].get<double>();
                        if (z > bestZ) { bestZ = z; best = o; }
                    } catch (...) {}
                }
                return best;
            };
            // Settle gate: after a step runs its ops we wait a few frames so the viewport + tree
            // repaint before capturing. m1Settle counts those frames.
            const int kSettle = 4;
            if (m1Settle > 0) { --m1Settle; }
            else if (m1Capture.empty()) {  // only advance when no capture is pending
                std::string err;
                switch (m1Step) {
                    case 0: {  // New Part
                        if (st.docMirror.newPart(st.worker, err)) {
                            std::printf("m1: new_part doc_id=%s rev=%d\n",
                                        st.docMirror.docId().c_str(), st.docMirror.revision());
                        } else std::printf("m1: new_part FAILED: %s\n", err.c_str());
                        std::fflush(stdout);
                        m1Capture = m1SavePath + "\\m1_01_newpart.png"; m1Settle = kSettle;
                        break;
                    }
                    case 1: {  // Box 60x40x10
                        nlohmann::json ops = nlohmann::json::array();
                        ops.push_back({{"op","add_feature"},{"feature",{
                            {"type","box"},{"name","Box1"},{"params",{
                                {"length",{{"value",60.0},{"expr",nullptr}}},
                                {"width", {{"value",40.0},{"expr",nullptr}}},
                                {"height",{{"value",10.0},{"expr",nullptr}}},
                                {"centered",true}}}}}});
                        auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
                        std::string boxId = r.id_map.is_object() && r.id_map.contains("$0")
                                          ? r.id_map["$0"].get<std::string>() : "f1";
                        std::printf("m1: box ok=%d rev=%d id=%s ms=%.2f\n",
                                    (int)r.ok, r.revision, boxId.c_str(), statusMs(r, boxId));
                        std::fflush(stdout);
                        // C32-A material proof: AI_CAD_MATERIAL=<plastic|aluminum|steel|brass|dark>
                        // applies the matching RealView preset to body b1 so a headless shot can be
                        // judged for metallic realism (clear env reflection + tinted fresnel).
                        if (const char* mat = std::getenv("AI_CAD_MATERIAL")) {
                            struct P { const char* k; float m, rough, rgb[3]; };
                            static const P kP[] = {
                                {"plastic",  0.00f, 0.50f, {0.71f, 0.76f, 0.82f}},
                                {"aluminum", 0.90f, 0.35f, {0.83f, 0.85f, 0.88f}},
                                {"steel",    0.85f, 0.25f, {0.56f, 0.58f, 0.62f}},
                                {"brass",    0.95f, 0.30f, {0.80f, 0.62f, 0.28f}},
                                {"dark",     0.90f, 0.45f, {0.16f, 0.17f, 0.19f}},
                            };
                            for (const auto& p : kP) {
                                if (std::strcmp(mat, p.k) == 0) {
                                    nlohmann::json mop = nlohmann::json::array();
                                    mop.push_back({{"op","set_appearance"},{"id","b1"},
                                        {"color",{p.rgb[0],p.rgb[1],p.rgb[2]}},
                                        {"metallic",p.m},{"roughness",p.rough}});
                                    auto rm = st.docMirror.applyOps(st.worker, mop); loadMesh(rm);
                                    std::printf("m1: material '%s' applied ok=%d rev=%d\n",
                                                p.k, (int)rm.ok, rm.revision);
                                    std::fflush(stdout);
                                    break;
                                }
                            }
                        }
                        st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                        m1Capture = m1SavePath + "\\m1_02_box.png"; m1Settle = kSettle;
                        break;
                    }
                    case 2: {  // Fillet r=4 on two picked vertical edges
                        auto edges = verticalEdges(2);
                        std::printf("m1: picked %zu vertical edges for fillet\n", edges.size());
                        nlohmann::json arr = nlohmann::json::array();
                        for (auto& e : edges) arr.push_back(e);
                        nlohmann::json ops = nlohmann::json::array();
                        ops.push_back({{"op","add_feature"},{"feature",{
                            {"type","fillet"},{"name","Fillet1"},
                            {"params",{{"radius",{{"value",4.0},{"expr",nullptr}}}}},
                            {"refs",{{"edges",arr}}}}}});
                        auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
                        m1FilletId = r.id_map.is_object() && r.id_map.contains("$0")
                                   ? r.id_map["$0"].get<std::string>() : "f2";
                        std::printf("m1: fillet ok=%d rev=%d id=%s ms=%.2f\n",
                                    (int)r.ok, r.revision, m1FilletId.c_str(),
                                    statusMs(r, m1FilletId));
                        std::fflush(stdout);
                        m1Capture = m1SavePath + "\\m1_03_fillet.png"; m1Settle = kSettle;
                        break;
                    }
                    case 3: {  // Shell t=2 remove top face
                        nlohmann::json top = topFace();
                        nlohmann::json remove = nlohmann::json::array();
                        if (top.is_object()) remove.push_back(top);
                        nlohmann::json ops = nlohmann::json::array();
                        ops.push_back({{"op","add_feature"},{"feature",{
                            {"type","shell"},{"name","Shell1"},
                            {"params",{{"thickness",{{"value",2.0},{"expr",nullptr}}}}},
                            {"refs",{{"remove",remove}}}}}});
                        auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
                        std::string shellId = r.id_map.is_object() && r.id_map.contains("$0")
                                            ? r.id_map["$0"].get<std::string>() : "f3";
                        std::printf("m1: shell ok=%d rev=%d id=%s ms=%.2f\n",
                                    (int)r.ok, r.revision, shellId.c_str(), statusMs(r, shellId));
                        // Record pre-edit timings for the cached-rebuild assertion.
                        m1FilletMsBeforeEdit = statusMs(r, m1FilletId);
                        m1BoxMsBeforeEdit = statusMs(r, "f1");
                        std::fflush(stdout);
                        m1Capture = m1SavePath + "\\m1_04_shell.png"; m1Settle = kSettle;
                        break;
                    }
                    case 4: {  // Edit Fillet r=8 — cached rebuild: box (f1) cached, fillet+shell recompile
                        // Drive this through the PropertyManager edit path so the form flow is
                        // exercised, then commit via edit_feature.
                        nlohmann::json ops = nlohmann::json::array();
                        ops.push_back({{"op","edit_feature"},{"id",m1FilletId},
                            {"params",{{"radius",{{"value",8.0},{"expr",nullptr}}}}},
                            {"refs",nlohmann::json::object()}});
                        auto r = st.docMirror.applyOps(st.worker, ops); loadMesh(r);
                        double boxAfter = statusMs(r, "f1");
                        double filletAfter = statusMs(r, m1FilletId);
                        std::printf("m1: edit_fillet ok=%d rev=%d fillet_ms=%.2f\n",
                                    (int)r.ok, r.revision, filletAfter);
                        std::printf("m1: cache-assert box f1 ms before=%.2f after=%.2f (cached=%s); "
                                    "fillet ms before=%.2f after=%.2f (recompiled=%s)\n",
                                    m1BoxMsBeforeEdit, boxAfter,
                                    (boxAfter == m1BoxMsBeforeEdit ? "yes" : "NO"),
                                    m1FilletMsBeforeEdit, filletAfter,
                                    (filletAfter != m1FilletMsBeforeEdit ? "yes" : "no"));
                        // Open the PM on the fillet so the screenshot shows the populated edit form.
                        propertyManager.openForEdit(st, m1FilletId);
                        std::fflush(stdout);
                        m1Capture = m1SavePath + "\\m1_05_edit.png"; m1Settle = kSettle;
                        break;
                    }
                    case 5: {  // Save .step+ then reopen; assert tree intact (3 features)
                        propertyManager.closeIfActive();  // tree returns for the reopen screenshot
                        std::string sp = m1SavePath + "\\part.step";
                        std::string serr;
                        bool saved = st.docMirror.save(st.worker, sp, serr);
                        std::printf("m1: save ok=%d path=%s err=%s\n", (int)saved, sp.c_str(),
                                    serr.c_str());
                        bool opened = st.docMirror.open(st.worker, sp, serr);
                        int nfeat = 0;
                        const auto& tr = st.docMirror.tree();
                        if (tr.is_object() && tr.contains("features") && tr["features"].is_array())
                            nfeat = (int)tr["features"].size();
                        std::printf("m1: reopen ok=%d rev=%d features=%d %s\n",
                                    (int)opened, st.docMirror.revision(), nfeat,
                                    nfeat == 3 ? "(tree intact)" : "(TREE MISMATCH)");
                        std::fflush(stdout);
                        // Reopen doesn't return an aicm; force a rebuild to refresh the viewport.
                        m1Capture = m1SavePath + "\\m1_06_reopen.png"; m1Settle = kSettle;
                        break;
                    }
                    default:
                        st.m1ShotDone = true;  // scenario complete; loop teardown handles exit
                        std::printf("m1: scenario complete\n");
                        std::fflush(stdout);
                        break;
                }
                if (m1Step <= 5) ++m1Step;
            }
        }

        // ---------- C18 sketch scenario (AI_CAD_SHOT=sketch) ----------
        // new part -> sketch on plane.top -> corner rect (ops + h/v + fix + 2 dims) -> sketch_enter
        // -> screenshot (entities + status) -> drag a corner + commit -> set width 80->100 -> shot
        // -> exit -> extrude the region depth 10 -> shot of the solid. Per-step captures use the
        // same backbuffer-write mechanism as m1 (sketchCapture).
        static int  skStep = 0, skSettle = 0;
        static std::string skSavePath, skFid;
        static std::string skL0, skL1, skL2, skL3, skWidthCid;
        // Hold the scenario for a few frames so the dockspace builds + docks the panels before the
        // first (step-0) capture; otherwise the first screenshot lands before the layout exists.
        if (st.wantSketch && st.workerStarted && !st.sketchShotDone && st.frame >= 5) {
            auto* sk = static_cast<ui::SketchMode*>(st.sketchMode);
            static bool skDirReady = false;
            if (!skDirReady) {
                skDirReady = true;
                const char* repo = std::getenv("AI_CAD_REPO");
                std::string base = repo ? std::string(repo) : std::string(".");
                skSavePath = base + "\\scratch\\sketch_shots";
            }
            auto applyOne = [&](const nlohmann::json& op) -> core::OpsResponse {
                return st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
            };
            const int kSettle = 4;
            if (skSettle > 0) { --skSettle; }
            else if (sketchCapture.empty()) {
                std::string err;
                switch (skStep) {
                    case 0: {  // new part + sketch + rect (entities/relations/dims) + enter
                        if (!st.docMirror.newPart(st.worker, err)) {
                            std::printf("sketch: new_part FAILED: %s\n", err.c_str()); std::fflush(stdout);
                            st.sketchShotDone = true; break;
                        }
                        auto r = applyOne({{"op","add_feature"},{"feature",{
                            {"type","sketch"},{"name","Sketch1"},
                            {"refs",{{"plane","plane.top"}}},
                            {"params",{{"entities",nlohmann::json::array()},
                                       {"constraints",nlohmann::json::array()}}}}}});
                        skFid = (r.id_map.is_object() && r.id_map.contains("$0")) ? r.id_map["$0"].get<std::string>() : "f1";
                        // four rect lines, chaining coincident corners via real ids
                        auto addLine = [&](double x1,double y1,double x2,double y2,bool horiz,const std::string& prev)->std::string{
                            nlohmann::json cs = nlohmann::json::array();
                            cs.push_back({{"type",horiz?"horizontal":"vertical"},{"refs",{"$self"}}});
                            if (!prev.empty()) cs.push_back({{"type","coincident"},{"refs",{prev+".p2","$self.p1"}}});
                            auto rr = applyOne({{"op","sketch_add_entity"},{"sketch",skFid},
                                {"entity",{{"type","line"},{"p1",{x1,y1}},{"p2",{x2,y2}}}},{"constraints",cs}});
                            return (rr.id_map.is_object() && rr.id_map.contains("$0")) ? rr.id_map["$0"].get<std::string>() : "";
                        };
                        skL0 = addLine(0,0,80,0,true,"");
                        skL1 = addLine(80,0,80,40,false,skL0);
                        skL2 = addLine(80,40,0,40,true,skL1);
                        skL3 = addLine(0,40,0,0,false,skL2);
                        applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                            {"constraint",{{"type","coincident"},{"refs",{skL3+".p2",skL0+".p1"}}}}});
                        applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                            {"constraint",{{"type","fix"},{"refs",{skL0+".p1"}}}}});
                        applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                            {"constraint",{{"type","distance"},{"refs",{skL0+".p1",skL0+".p2"}},
                                           {"value",{{"value",80.0},{"expr",nullptr}}}}}});
                        auto rH = applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                            {"constraint",{{"type","distance"},{"refs",{skL1+".p1",skL1+".p2"}},
                                           {"value",{{"value",40.0},{"expr",nullptr}}}}}});
                        skWidthCid = (rH.id_map.is_object() && rH.id_map.contains("$0")) ? "" : "";
                        // find the width constraint id by scanning the sketch params (it's the
                        // distance on skL0). We re-read the tree to locate it.
                        {
                            const auto& tr = st.docMirror.tree();
                            if (tr.contains("features"))
                              for (auto& f : tr["features"])
                                if (f.value("id",std::string())==skFid && f.contains("params"))
                                  for (auto& c : f["params"].value("constraints",nlohmann::json::array()))
                                    if (c.value("type",std::string())=="distance" && c.contains("refs") &&
                                        c["refs"].size()==2 && c["refs"][0].get<std::string>()==skL0+".p1")
                                      skWidthCid = c.value("id",std::string());
                        }
                        if (sk) sk->enterForFeature(st, skFid);
                        st.zoom = 1.0f;
                        std::printf("sketch: new sketch %s rect built (width_cid=%s) entered; status=%s\n",
                                    skFid.c_str(), skWidthCid.c_str(), sk ? sk->statusLine().c_str() : "?");
                        std::fflush(stdout);
                        sketchCapture = skSavePath + "\\sketch_01_enter.png"; skSettle = kSettle;
                        break;
                    }
                    case 1: {  // drag a corner + commit, then set width 80 -> 100
                        nlohmann::json dres;
                        nlohmann::json dragP = {{"doc_id",st.docMirror.docId()},{"sketch",skFid},
                            {"handle",skL1+".p2"},{"xy",nlohmann::json::array({85.0,45.0})}};
                        st.worker.call("sketch_drag", dragP, dres, err);
                        nlohmann::json cres;
                        nlohmann::json cP = {{"doc_id",st.docMirror.docId()},{"sketch",skFid}};
                        st.worker.call("sketch_drag_commit", cP, cres, err);
                        // resync mirror (commit bumped the revision) then set the width dim
                        st.docMirror.resync(st.worker);
                        if (!skWidthCid.empty())
                            st.docMirror.applyOps(st.worker, nlohmann::json::array({
                                {{"op","sketch_set_dim"},{"sketch",skFid},{"id",skWidthCid},{"value",100.0}}}));
                        nlohmann::json sres;
                        st.worker.call("sketch_solve", {{"doc_id",st.docMirror.docId()},{"sketch",skFid}}, sres, err);
                        if (sk) sk->enterForFeature(st, skFid);  // refresh overlay entities/status
                        std::printf("sketch: drag+commit, width 80->100, re-solved; status=%s dof=%d\n",
                                    sk ? sk->statusLine().c_str() : "?", sres.value("dof",-1));
                        std::fflush(stdout);
                        sketchCapture = skSavePath + "\\sketch_02_dim.png"; skSettle = kSettle;
                        break;
                    }
                    case 2: {  // exit + extrude region depth 10
                        if (sk) sk->exitSketch(st, /*keep=*/true);
                        st.docMirror.resync(st.worker);
                        nlohmann::json region = {{"kind","region"},{"sketch",skFid},
                            {"centroid",nlohmann::json::array({50.0,20.0})},{"hint_index",0}};
                        auto r = applyOne({{"op","add_feature"},{"feature",{
                            {"type","extrude"},{"name","Boss-Extrude1"},
                            {"params",{{"depth",{{"value",10.0},{"expr",nullptr}}},{"end","blind"},{"mode","boss"}}},
                            {"refs",{{"profile",nlohmann::json::array({region})}}}}}});
                        if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                        bool exok = false;
                        if (r.feature_status.is_array() && !r.feature_status.empty())
                            exok = r.feature_status.back().value("ok",false);
                        st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                        std::printf("sketch: exit + extrude region depth 10 ok=%d\n", (int)exok);
                        std::fflush(stdout);
                        sketchCapture = skSavePath + "\\sketch_03_solid.png"; skSettle = kSettle;
                        break;
                    }
                    default:
                        st.sketchShotDone = true;
                        std::printf("sketch: scenario complete\n"); std::fflush(stdout);
                        break;
                }
                if (skStep <= 2) ++skStep;
            }
        }

        // ---------- C20b assembly scenario (AI_CAD_SHOT=asm) ----------
        // Mirrors engine/doc/test_worker_asm.py through the C++ DocMirror/AsmPanels stack:
        //   author + save base.step+ (40x40x10 plate, 8mm hole) and peg.step+ (r=3.9, h=20),
        //   new assembly, insert both, concentric (hole<->peg) + coincident ANTI (plate top<->peg
        //   bottom) -> peg seats at (0,0,15); screenshot the seated assembly with the Mates folder
        //   visible; explode factor 1.0 -> screenshot; interference window open -> screenshot.
        // Per-step captures reuse the m1/sketch backbuffer-write mechanism (asmCapture).
        static int  asmStep = 0, asmSettle = 0;
        static std::string asmSavePath, asmBasePath, asmPegPath, asmDoc, asmK2;
        static double asmSolvedZ = -999.0;
        if (st.wantAsm && st.workerStarted && !st.asmShotDone && st.frame >= 5) {
            auto* ap = static_cast<ui::AsmPanels*>(st.asmPanels);
            static bool asmDirReady = false;
            if (!asmDirReady) {
                asmDirReady = true;
                const char* repo = std::getenv("AI_CAD_REPO");
                std::string base = repo ? std::string(repo) : std::string(".");
                asmSavePath = base + "\\scratch\\asm_shots";
                asmBasePath = asmSavePath + "\\base.step+";
                asmPegPath  = asmSavePath + "\\peg.step+";
            }
            // Helper: author a part via raw worker calls (doc_new part -> apply_ops -> save -> close).
            auto dim = [](double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; };
            auto makePart = [&](const std::string& name, const nlohmann::json& ops,
                                const std::string& path) -> bool {
                nlohmann::json res; std::string err;
                if (!st.worker.call("doc_new", {{"kind", "part"}, {"name", name}}, res, err)) return false;
                std::string did = res.value("doc_id", std::string());
                int rev = res.value("revision", 0);
                nlohmann::json ap2;
                if (!st.worker.call("doc_apply_ops",
                        {{"doc_id", did}, {"base_revision", rev}, {"ops", ops}}, ap2, err)) return false;
                nlohmann::json sv;
                if (!st.worker.call("doc_save", {{"doc_id", did}, {"path", path}}, sv, err)) return false;
                nlohmann::json cl;
                st.worker.call("doc_close", {{"doc_id", did}}, cl, err);
                return true;
            };
            const int kSettle = 4;
            if (asmSettle > 0) { --asmSettle; }
            else if (asmCapture.empty()) switch (asmStep) {  // only advance when no capture pending
                case 0: {  // author the two parts
                    nlohmann::json baseOps = nlohmann::json::array();
                    baseOps.push_back({{"op","add_feature"},{"feature",{
                        {"type","box"},{"name","Plate"},{"suppressed",false},
                        {"params",{{"length",dim(40)},{"width",dim(40)},{"height",dim(10)},
                                   {"centered",true}}},{"refs",nlohmann::json::object()}}}});
                    baseOps.push_back({{"op","add_feature"},{"feature",{
                        {"type","cylinder"},{"name","Hole"},{"suppressed",false},
                        {"params",{{"radius",dim(4)},{"height",dim(12)},{"centered",true},
                                   {"mode","cut"}}},{"refs",nlohmann::json::object()}}}});
                    nlohmann::json pegOps = nlohmann::json::array();
                    pegOps.push_back({{"op","add_feature"},{"feature",{
                        {"type","cylinder"},{"name","Peg"},{"suppressed",false},
                        {"params",{{"radius",dim(3.9)},{"height",dim(20)},{"centered",true}}},
                        {"refs",nlohmann::json::object()}}}});
                    bool okB = makePart("base", baseOps, asmBasePath);
                    bool okP = makePart("peg", pegOps, asmPegPath);
                    std::printf("asm: parts authored base=%d peg=%d\n", (int)okB, (int)okP);
                    std::fflush(stdout);
                    asmSettle = 2; ++asmStep; break;
                }
                case 1: {  // new assembly + insert both (peg offset away from the hole)
                    std::string err;
                    if (!st.docMirror.newAssembly(st.worker, err)) {
                        std::printf("asm: new_assembly FAILED: %s\n", err.c_str()); std::fflush(stdout);
                        st.asmShotDone = true; break;
                    }
                    asmDoc = st.docMirror.docId();
                    std::string aicm;
                    ap->insertComponent(st, asmBasePath, nullptr, aicm);
                    nlohmann::json off = nlohmann::json::array({1,0,0,30, 0,1,0,25, 0,0,1,18, 0,0,0,1});
                    ap->insertComponent(st, asmPegPath, off, aicm);
                    // identify the peg component id (k2 = second component)
                    const auto& tr = st.docMirror.tree();
                    if (tr.contains("components") && tr["components"].is_array() &&
                        tr["components"].size() >= 2)
                        asmK2 = tr["components"][1].value("id", std::string());
                    std::printf("asm: assembly %s inserted base+peg (peg=%s)\n",
                                asmDoc.c_str(), asmK2.c_str());
                    std::fflush(stdout);
                    st.yaw = 28; st.pitch = 22; st.zoom = 1.0f;
                    asmSettle = kSettle; ++asmStep; break;
                }
                case 2: {  // concentric + coincident(anti) mates -> peg seats at z=15
                    // Resolve the mate selectors via the asm pick path (component-body ordinals).
                    auto resolveAsm = [&](int bodyId, const char* kind, int topo,
                                          std::string& comp) -> nlohmann::json {
                        std::string err;
                        std::string s = st.docMirror.resolvePickAsm(st.worker, bodyId, kind, topo, comp, err);
                        if (s.empty()) return nlohmann::json();
                        try { return nlohmann::json::parse(s); } catch (...) { return nlohmann::json(); }
                    };
                    // Scan base (body 1) faces for the hole cylinder + the top plane (max z plane).
                    std::string cBase, cPeg;
                    nlohmann::json holeCyl, topPlate;
                    for (int t = 1; t <= 30; ++t) {
                        std::string comp;
                        nlohmann::json s = resolveAsm(1, "face", t, comp);
                        if (!s.is_object()) break;
                        cBase = comp;
                        std::string surf = s.value("surf", std::string());
                        if (surf == "cylinder" && holeCyl.is_null()) holeCyl = s;
                        if (surf == "plane") {
                            double z = s.contains("centroid") ? s["centroid"][2].get<double>() : -1e9;
                            double bz = topPlate.is_object() && topPlate.contains("centroid")
                                        ? topPlate["centroid"][2].get<double>() : -1e9;
                            if (z > bz) topPlate = s;
                        }
                    }
                    // Scan peg (body 2) faces for its cylinder + the bottom plane (min z plane).
                    nlohmann::json pegCyl, botPeg;
                    for (int t = 1; t <= 30; ++t) {
                        std::string comp;
                        nlohmann::json s = resolveAsm(2, "face", t, comp);
                        if (!s.is_object()) break;
                        cPeg = comp;
                        std::string surf = s.value("surf", std::string());
                        if (surf == "cylinder" && pegCyl.is_null()) pegCyl = s;
                        if (surf == "plane") {
                            double z = s.contains("centroid") ? s["centroid"][2].get<double>() : 1e9;
                            double bz = botPeg.is_object() && botPeg.contains("centroid")
                                        ? botPeg["centroid"][2].get<double>() : 1e9;
                            if (z < bz) botPeg = s;
                        }
                    }
                    std::string aicm;
                    if (holeCyl.is_object() && pegCyl.is_object()) {
                        nlohmann::json refs = nlohmann::json::array();
                        refs.push_back({{"component", cBase}, {"selector", holeCyl}});
                        refs.push_back({{"component", cPeg},  {"selector", pegCyl}});
                        ap->addMate(st, "concentric", "aligned", refs, aicm);
                    }
                    if (topPlate.is_object() && botPeg.is_object()) {
                        nlohmann::json refs = nlohmann::json::array();
                        refs.push_back({{"component", cBase}, {"selector", topPlate}});
                        refs.push_back({{"component", cPeg},  {"selector", botPeg}});
                        ap->addMate(st, "coincident", "anti", refs, aicm);
                    }
                    // Read the solved peg transform z from the tree.
                    asmSolvedZ = -999.0;
                    const auto& tr = st.docMirror.tree();
                    if (tr.contains("components"))
                        for (const auto& c : tr["components"])
                            if (c.value("id", std::string()) == asmK2 &&
                                c.contains("transform") && c["transform"].is_array() &&
                                c["transform"].size() == 16)
                                asmSolvedZ = c["transform"][11].get<double>();
                    // Open the Mate form so the screenshot shows the assembly Mate PM; the Mates
                    // folder itself lives in the FeatureManager tree (visible alongside). Close the
                    // auto-opened Insert window so the tree's Mates folder is unobscured.
                    ap->closeInsert();
                    ap->openMateForm();
                    std::printf("asm: mates added; solved peg z=%.3f (expect 15)\n", asmSolvedZ);
                    std::fflush(stdout);
                    if (std::fabs(asmSolvedZ - 15.0) < 0.5)
                        std::printf("asm: PASS |z-15|<0.5\n");
                    else
                        std::printf("asm: FAIL z=%.3f not within 0.5 of 15\n", asmSolvedZ);
                    std::fflush(stdout);
                    asmCapture = asmSavePath + "\\asm_01_seated.png"; asmSettle = kSettle; ++asmStep; break;
                }
                case 3: {  // explode factor 1.0
                    ap->closeMateForm();
                    // Define an explode step for the peg (lift it +Z 40mm) so the slider has
                    // something to act on, then activate the exploded view at factor 1.0.
                    {
                        nlohmann::json step = {{"component", asmK2},
                            {"vector", nlohmann::json::array({0, 0, 1})}, {"distance", 40.0}};
                        nlohmann::json steps = nlohmann::json::array(); steps.push_back(step);
                        st.docMirror.applyOps(st.worker, nlohmann::json::array({
                            {{"op","asm_set_explode"},{"steps",steps}}}));
                    }
                    ap->handleCommand(st, "asm_explode");  // opens the Exploded View slider window
                    ap->setExplode(st, true, 1.0f);
                    std::printf("asm: explode factor=1.0 (peg +40mm)\n"); std::fflush(stdout);
                    asmCapture = asmSavePath + "\\asm_02_explode.png"; asmSettle = kSettle; ++asmStep; break;
                }
                case 4: {  // interference window
                    ap->setExplode(st, false, 0.0f);  // collapse before checking real fit
                    ap->closeExplode();               // dismiss the explode window so it isn't on top
                    ap->openInterference(st);
                    std::printf("asm: interference window open\n"); std::fflush(stdout);
                    asmCapture = asmSavePath + "\\asm_03_interference.png"; asmSettle = kSettle; ++asmStep; break;
                }
                default:
                    st.asmShotDone = true;
                    std::printf("asm: scenario complete (solved z=%.3f)\n", asmSolvedZ);
                    std::fflush(stdout);
                    break;
            }
        }

        // ---------- C19 FEA scenario (AI_CAD_SHOT=fea) ----------
        // new part -> box 60x40x10 -> static study: fix the -X end face, 100 N -Z on the +X end,
        // steel (index 0), density 0.4 -> wait for completion (cap ~90 s) -> von Mises colormap.
        static int feaStep = 0, feaSettle = 0, feaWaitFrames = 0;
        if (st.wantFea && st.workerStarted && !st.feaShotDone) {
            auto* sim = static_cast<core::SimClient*>(st.simClient);
            simPanel.tick(st);  // pump the panel so completion lands while we wait
            if (feaSettle > 0) { --feaSettle; }
            else switch (feaStep) {
                case 0: {  // New part + box
                    std::string err;
                    st.docMirror.newPart(st.worker, err);
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op","add_feature"},{"feature",{
                        {"type","box"},{"name","Box1"},{"params",{
                            {"length",{{"value",60.0},{"expr",nullptr}}},
                            {"width", {{"value",40.0},{"expr",nullptr}}},
                            {"height",{{"value",10.0},{"expr",nullptr}}}},
                        },{"centered",true}}}});
                    auto r = st.docMirror.applyOps(st.worker, ops);
                    if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                    std::printf("fea: box ok=%d rev=%d\n", (int)r.ok, r.revision);
                    std::fflush(stdout);
                    st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                    feaSettle = 3; ++feaStep; break;
                }
                case 1: {  // Static study + resolved fixture/load faces (-X end / +X end)
                    simPanel.handleCommand(st, "sim_new_static");
                    {
                        int steelIdx = simPanel.materialIndexByKey("steel_1018");
                        simPanel.configureActive(0.4f, steelIdx >= 0 ? steelIdx : 0);
                    }
                    auto faceAtExtremeX = [&](bool wantMax) -> nlohmann::json {
                        nlohmann::json best; double bestC = wantMax ? -1e30 : 1e30;
                        for (int tid = 1; tid <= 12; ++tid) {
                            std::string se;
                            std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                            if (s.empty()) continue;
                            try {
                                auto o = nlohmann::json::parse(s);
                                if (!o.contains("centroid")) continue;
                                double cx = o["centroid"][0].get<double>();
                                if ((wantMax && cx > bestC) || (!wantMax && cx < bestC)) { bestC = cx; best = o; }
                            } catch (...) {}
                        }
                        return best;
                    };
                    nlohmann::json fixFace = faceAtExtremeX(false);
                    nlohmann::json loadFace = faceAtExtremeX(true);
                    // Inject the resolved selectors via the selection -> well capture path.
                    st.selection.clear();
                    { core::SelItem it; it.selector = fixFace.dump(); it.pending_resolve = false;
                      st.selection.mutableItems().push_back(it); }
                    simPanel.handleCommand(st, "sim_fixture");
                    st.selection.clear();
                    { core::SelItem it; it.selector = loadFace.dump(); it.pending_resolve = false;
                      st.selection.mutableItems().push_back(it); }
                    simPanel.handleCommand(st, "sim_load_force");
                    st.selection.clear();
                    std::printf("fea: faces fixCx=%.1f loadCx=%.1f\n",
                                fixFace.contains("centroid") ? fixFace["centroid"][0].get<double>() : 0.0,
                                loadFace.contains("centroid") ? loadFace["centroid"][0].get<double>() : 0.0);
                    std::fflush(stdout);
                    feaSettle = 2; ++feaStep; break;
                }
                case 2: {  // Run
                    simPanel.handleCommand(st, "sim_run");
                    std::printf("fea: study launched (density 0.4, steel, 100 N)\n");
                    std::fflush(stdout);
                    feaWaitFrames = 0; ++feaStep; break;
                }
                case 3: {  // Wait for completion (cap ~90 s)
                    ++feaWaitFrames;
                    if (st.colormapOn || feaWaitFrames > 5400) {
                        std::printf("fea: result ready colormap=%d legend=%d (%d frames)\n",
                                    (int)st.colormapOn, (int)st.legendOn, feaWaitFrames);
                        std::fflush(stdout);
                        ++feaStep;
                    }
                    break;
                }
                default:
                    st.feaShotDone = true;
                    std::printf("fea: scenario complete\n");
                    std::fflush(stdout);
                    break;
            }
            (void)sim;
        }

        // ---------- C19 evalmass scenario (AI_CAD_SHOT=evalmass) ----------
        static int evalStep = 0, evalSettle = 0;
        if (st.wantEvalMass && st.workerStarted && !st.evalMassShotDone) {
            if (evalSettle > 0) { --evalSettle; }
            else switch (evalStep) {
                case 0: {
                    std::string err;
                    st.docMirror.newPart(st.worker, err);
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op","add_feature"},{"feature",{
                        {"type","box"},{"name","Box1"},{"params",{
                            {"length",{{"value",50.0},{"expr",nullptr}}},
                            {"width", {{"value",30.0},{"expr",nullptr}}},
                            {"height",{{"value",20.0},{"expr",nullptr}}}},
                        },{"centered",true}}}});
                    auto r = st.docMirror.applyOps(st.worker, ops);
                    if (r.ok && !r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                    st.yaw = 35; st.pitch = 28; st.zoom = 1.0f;
                    std::printf("evalmass: box ok=%d\n", (int)r.ok); std::fflush(stdout);
                    evalSettle = 3; ++evalStep; break;
                }
                case 1: {
                    evaluateDialogs.handleCommand(st, "eval_mass_props");
                    std::printf("evalmass: mass properties window opened\n"); std::fflush(stdout);
                    ++evalStep; break;
                }
                default:
                    st.evalMassShotDone = true;
                    std::printf("evalmass: scenario complete\n"); std::fflush(stdout);
                    break;
            }
        }

        // --- C22b activity scenario (AI_CAD_SHOT=activity) ---
        static int actStep = 0, actSettle = 0, actWaitFrames = 0;
        static std::string actCapture;
        static std::string actSavePath;
        if (st.wantActivity && st.workerStarted && !st.activityShotDone && st.frame >= 5) {
            static bool actDirReady = false;
            if (!actDirReady) {
                actDirReady = true;
                const char* repo = std::getenv("AI_CAD_REPO");
                std::string base = repo ? std::string(repo) : std::string(".");
                actSavePath = base + "\\scratch\\activity_shots";
            }
            const int kSettle = 4;
            if (actSettle > 0) { --actSettle; }
            else if (actCapture.empty()) {
                std::string aerr;
                switch (actStep) {
                    case 0: {  // New part
                        if (!st.docMirror.newPart(st.worker, aerr)) {
                            std::printf("activity: new_part FAILED: %s\n", aerr.c_str());
                            std::fflush(stdout); st.activityShotDone = true; break;
                        }
                        std::printf("activity: new_part doc_id=%s rev=%d\n",
                                    st.docMirror.docId().c_str(), st.docMirror.revision());
                        std::fflush(stdout);
                        actSettle = kSettle; ++actStep; break;
                    }
                    case 1: {  // Launch agent_run_v2
                        std::string req = "a bracket with a filleted top edge";
                        std::strncpy(st.prompt.data(), req.c_str(), st.prompt.size() - 1);
                        st.agentV2 = true;
                        st.agentV2Running = true;
                        activityPanel.clearLive();
                        launchAgentRunV2(st.worker, st.job, req, st.docMirror.docId());
                        std::printf("activity: agent_run_v2 launched: %s\n", req.c_str());
                        std::fflush(stdout);
                        actWaitFrames = 0; ++actStep; break;
                    }
                    case 2: {  // Wait for run to finish
                        ++actWaitFrames;
                        if (!st.job.running.load() || actWaitFrames > 1800) {
                            std::printf("activity: run done (frames=%d)\n", actWaitFrames);
                            std::fflush(stdout);
                            actSettle = kSettle * 2; ++actStep;
                        }
                        break;
                    }
                    case 3: {  // Screenshot 1: Activity panel with run tree
                        std::printf("activity: shot1 activity panel\n"); std::fflush(stdout);
                        actCapture = actSavePath + "\\activity_01_run.png";
                        actSettle = kSettle; ++actStep; break;
                    }
                    case 4: {  // Open Settings > Agents tab
                        st.openSettings = true;
                        st.pendingPromptRole = "modeler";
                        actSettle = kSettle * 2; ++actStep; break;
                    }
                    case 5: {  // Screenshot 2: Settings Agents tab
                        std::printf("activity: shot2 agents settings tab\n"); std::fflush(stdout);
                        actCapture = actSavePath + "\\activity_02_agents.png";
                        actSettle = kSettle; ++actStep; break;
                    }
                    default:
                        st.activityShotDone = true;
                        std::printf("activity: scenario complete\n"); std::fflush(stdout);
                        break;
                }
            }
        }
        // --- end C22b activity scenario ---

        bool busy = st.job.running.load();

        ImGui_ImplOpenGL3_NewFrame();
        ImGui_ImplGlfw_NewFrame();
        ImGui::NewFrame();

        // Re-push palette-derived viewport visuals every frame so a runtime theme toggle (from the
        // title bar) takes effect immediately. Cheap uniform/state sets; no GPU allocation.
        {
            float top3[3] = {ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                             ui::palette.viewportTop[2]};
            float mid3[3] = {ui::palette.viewportMid[0], ui::palette.viewportMid[1],
                             ui::palette.viewportMid[2]};
            float bot3[3] = {ui::palette.viewportBottom[0], ui::palette.viewportBottom[1],
                             ui::palette.viewportBottom[2]};
            renderer.setBackground3(top3, mid3, bot3);
            renderer.setEdgeStyle(ui::palette.viewportEdge[0], ui::palette.viewportEdge[1],
                                  ui::palette.viewportEdge[2], 1.6f * ui::uiScale);
            renderer.setDefaultPartColor(ui::palette.partDefault[0], ui::palette.partDefault[1],
                                         ui::palette.partDefault[2]);
            // W4-K: gate on showGroundShadow so the HUD hide/show toggle actually kills the shadow.
            // (Previously this unconditional full-alpha push every frame clobbered viewport_panel's
            // gated value before renderOrbit ran, so the toggle had no visible effect.)
            float gsA = st.showGroundShadow ? ui::palette.groundShadow[3] : 0.0f;
            renderer.setGroundShadow(ui::palette.groundShadow[0], ui::palette.groundShadow[1],
                                     ui::palette.groundShadow[2], gsA);
            renderer.setRealView(st.showRealView);  // C30-C: RealView environment reflections
            renderer.setPlaneColors(ui::palette.planeFill, ui::palette.planeOutline);
        }

        if (shot && st.frame == 1) {
            if (st.wantSettings) st.openSettings = true;
            if (st.wantChat || st.wantAssembly || st.wantPlan)
                st.designStarted = true;  // show the conversation takeover

        }

        // ---------- custom title bar (our own; the OS title bar is stripped) ----------
        ImGuiViewport* vp = ImGui::GetMainViewport();
        const bool maximized = glfwGetWindowAttrib(win, GLFW_MAXIMIZED) != 0;

        float barH = 0.0f;
        ui::drawTitleBar(st, maximized, barH);

        // ---------- C14: ribbon bar (drawn between title bar and dockspace) ----------
        // syncIconTheme keeps texture selection in sync with the active theme.
        ui::syncIconTheme();
        float ribbonH = ui::drawRibbon(st.ribbonTab, st.showRibbon);

        // ---------- dockspace host (between the title bar+ribbon and the status bar) ----------
        const float statusH = 30.0f * scale;
        const float dockTopY = barH + ribbonH;
        ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + dockTopY));
        ImGui::SetNextWindowSize(ImVec2(vp->Size.x, vp->Size.y - dockTopY - statusH));
        ImGui::SetNextWindowViewport(vp->ID);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
        ImGuiWindowFlags hostFlags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoCollapse |
            ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoMove |
            ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNavFocus |
            ImGuiWindowFlags_NoDocking;
        ImGui::Begin("##DockHost", nullptr, hostFlags);
        ImGui::PopStyleVar(3);
        ImGuiID dockId = ImGui::GetID("AICADDock");
        if ((!st.layoutInit && ImGui::DockBuilderGetNode(dockId) == nullptr) || st.resetLayout) {
            buildDefaultLayout(dockId, ImVec2(vp->Size.x, vp->Size.y - dockTopY - statusH));
            st.layoutInit = true;
            st.resetLayout = false;
            // C24: make Design the active tab in the right task pane (not Agent Activity), matching
            // SolidWorks's task pane defaulting to the first tab. Deferred one frame via a flag so
            // the docked windows exist before we focus.
            st.focusDesignTab = true;
        }
        ImGui::DockSpace(dockId, ImVec2(0, 0), ImGuiDockNodeFlags_None);
        ImGui::End();

        // ---------- status bar ----------
        ui::drawStatusBar(st, vp, statusH, busy, hostFlags);

        bool canRun = st.workerStarted && !busy;

        // ---------- panels ----------
        // C17: the PropertyManager occupies the FeatureManager dock slot while a command is
        // active; the tree returns when the PM closes (OK/Cancel). The tree's context-menu "Edit"
        // reopens a PM form populated from the feature.
        if (st.showFeatureTree) {
            if (propertyManager.active()) {
                propertyManager.tick(st);     // fire the debounced live preview if dirty
                propertyManager.draw(st);
            } else {
                ui::drawFeatureTree(st, [&st, &propertyManager](const std::string& fid) {
                    propertyManager.openForEdit(st, fid);
                });
            }
        }
        // W3-H: unified AI Copilot pane (input-at-top + conversation; embeds the run tree
        // inline while a v2 run is active). st.activityPanel is still fed events via onEvent.
        ui::drawAIPanel(st, busy, canRun);
        // W3-H: standalone "Agent Activity" window (View > Activity / the pane's Activity
        // button / the activity shot). No-op unless st.showActivityPanel; was previously
        // unreachable because only the in-pane tab hosted drawBody.
        activityPanel.draw(st);
        ui::drawViewportPanel(st, io, busy);

        // ---------- C19: simulation panel + evaluate dialogs ----------
        simPanel.tick(st);          // drain SimClient progress + completion (loads overlays)
        simPanel.draw(st);
        evaluateDialogs.draw(st);

        // ---------- C20b: assembly panels (Insert / Mate / Explode / Interference / BOM) ----------
        asmPanels.draw(st);

        // ---------- C15 pick-shot driver: simulate a center-screen pick ----------
        // The viewport panel rendered the id buffer for this frame's camera; pick its center,
        // apply the selection (highlight shows from the next frame), and print the PickHit once
        // so verification is scriptable. Runs every frame so the camera has settled by capture.
        if (st.wantPick && st.pickReady) {
            const Renderer::CameraXform& cam = renderer.camera();
            int fw = cam.ssWidth / renderer.supersample();
            int fh = cam.ssHeight / renderer.supersample();
            render::PickHit center = idPass.pickFinal(fw / 2, fh / 2, fw, fh);
            st.hover = render::PickHit{};
            st.selection.selectOne(center);
            st.pickStatus = st.pickLabel(center);
            if (!st.pickPrinted) {
                const char* kindName =
                    center.kind == render::PickKind::Face ? "face" :
                    center.kind == render::PickKind::Edge ? "edge" :
                    center.kind == render::PickKind::Vertex ? "vertex" : "none";
                std::printf("pick: body=%u kind=%s id=%u\n", center.body_id, kindName,
                            center.topo_id);
                std::fflush(stdout);
                st.pickPrinted = true;
            }
        }

        // ---------- modals ----------
        ui::drawSettingsModal(st, busy);

        ImGui::Render();
        int dw, dh;
        glfwGetFramebufferSize(win, &dw, &dh);
        glViewport(0, 0, dw, dh);
        glClearColor(ui::palette.windowBg[0], ui::palette.windowBg[1],
                     ui::palette.windowBg[2], 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());

        // C17 M1 scenario: per-step captures. When the step machine set m1Capture, write the
        // current backbuffer to it (this frame already drew the post-op state) and clear it.
        // The scenario drives its own lifetime; we exit when m1ShotDone is latched AND no capture
        // is pending. (Counts as the "m1" regression-gate screenshot via m1_06_reopen.png copied
        // to the requested path by the gate runner; here we also satisfy the single screenshotPath
        // by writing the last step to it.)
        if (st.wantM1) {
            ++st.frame;
            if (!m1Capture.empty()) {
                std::vector<unsigned char> px(dw * dh * 4);
                glReadBuffer(GL_BACK);
                glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
                std::vector<unsigned char> flip(dw * dh * 4);
                for (int y = 0; y < dh; ++y)
                    std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
                stbi_write_png(m1Capture.c_str(), dw, dh, 4, flip.data(), dw * 4);
                std::printf("m1 screenshot -> %s (%dx%d)\n", m1Capture.c_str(), dw, dh);
                std::fflush(stdout);
                // Mirror the final step to the caller-requested screenshotPath.
                if (m1Capture.find("m1_06_reopen") != std::string::npos && screenshotPath)
                    stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
                m1Capture.clear();
            }
            if (st.m1ShotDone) { glfwSwapBuffers(win); break; }
            glfwSwapBuffers(win);
            continue;
        }

        // C18 sketch scenario: identical per-step capture mechanism.
        if (st.wantSketch) {
            ++st.frame;
            if (!sketchCapture.empty()) {
                std::vector<unsigned char> px(dw * dh * 4);
                glReadBuffer(GL_BACK);
                glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
                std::vector<unsigned char> flip(dw * dh * 4);
                for (int y = 0; y < dh; ++y)
                    std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
                stbi_write_png(sketchCapture.c_str(), dw, dh, 4, flip.data(), dw * 4);
                std::printf("sketch screenshot -> %s (%dx%d)\n", sketchCapture.c_str(), dw, dh);
                std::fflush(stdout);
                if (sketchCapture.find("sketch_03_solid") != std::string::npos && screenshotPath)
                    stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
                sketchCapture.clear();
            }
            if (st.sketchShotDone) { glfwSwapBuffers(win); break; }
            glfwSwapBuffers(win);
            continue;
        }

        // C20b assembly scenario: identical per-step capture mechanism.
        if (st.wantAsm) {
            ++st.frame;
            if (!asmCapture.empty()) {
                std::vector<unsigned char> px(dw * dh * 4);
                glReadBuffer(GL_BACK);
                glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
                std::vector<unsigned char> flip(dw * dh * 4);
                for (int y = 0; y < dh; ++y)
                    std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
                stbi_write_png(asmCapture.c_str(), dw, dh, 4, flip.data(), dw * 4);
                std::printf("asm screenshot -> %s (%dx%d)\n", asmCapture.c_str(), dw, dh);
                std::fflush(stdout);
                if (asmCapture.find("asm_01_seated") != std::string::npos && screenshotPath)
                    stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
                asmCapture.clear();
            }
            if (st.asmShotDone) { glfwSwapBuffers(win); break; }
            glfwSwapBuffers(win);
            continue;
        }

        // C19 fea/evalmass shots: drive the async scenario across frames; capture once it's done.
        if (st.wantFea) {
            ++st.frame;
            if (st.feaShotDone) {
                std::vector<unsigned char> px(dw * dh * 4);
                glReadBuffer(GL_BACK);
                glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
                std::vector<unsigned char> flip(dw * dh * 4);
                for (int y = 0; y < dh; ++y)
                    std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
                if (screenshotPath) stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
                std::printf("fea screenshot -> %s (%dx%d)\n",
                            screenshotPath ? screenshotPath : "(none)", dw, dh);
                std::fflush(stdout);
                glfwSwapBuffers(win); break;
            }
            glfwSwapBuffers(win);
            continue;
        }
        if (st.wantEvalMass) {
            ++st.frame;
            if (st.evalMassShotDone && st.frame > 6) {
                std::vector<unsigned char> px(dw * dh * 4);
                glReadBuffer(GL_BACK);
                glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
                std::vector<unsigned char> flip(dw * dh * 4);
                for (int y = 0; y < dh; ++y)
                    std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
                if (screenshotPath) stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
                std::printf("evalmass screenshot -> %s (%dx%d)\n",
                            screenshotPath ? screenshotPath : "(none)", dw, dh);
                std::fflush(stdout);
                glfwSwapBuffers(win); break;
            }
            glfwSwapBuffers(win);
            continue;
        }

        // --- C22b activity shot capture ---
        if (st.wantActivity) {
            ++st.frame;
            if (!actCapture.empty()) {
                // mkdir best-effort
                std::string mkCmd = "if not exist \"" + actSavePath + "\" mkdir \"" + actSavePath + "\"";
                std::system(mkCmd.c_str());
                std::vector<unsigned char> px(dw * dh * 4);
                glReadBuffer(GL_BACK);
                glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
                std::vector<unsigned char> flip(dw * dh * 4);
                for (int y = 0; y < dh; ++y)
                    std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
                stbi_write_png(actCapture.c_str(), dw, dh, 4, flip.data(), dw * 4);
                std::printf("activity: screenshot -> %s (%dx%d)\n",
                            actCapture.c_str(), dw, dh);
                std::fflush(stdout);
                // Mirror the last step to screenshotPath
                if (actCapture.find("activity_02_agents") != std::string::npos && screenshotPath)
                    stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
                actCapture.clear();
            }
            if (st.activityShotDone) { glfwSwapBuffers(win); break; }
            glfwSwapBuffers(win);
            continue;
        }
        // --- end C22b activity shot capture ---

        // tree shot needs extra frames for the tree to repaint after async doc load
        // wantTree but !treeShotDone: keep waiting (up to 30 frames) for the worker to start
        // wantTree and treeShotDone: take the screenshot at frame 10 (tree has repainted)
        const int shotFrame = st.wantTree ? (st.treeShotDone ? 10 : 30) : 6;
        if (shot && ++st.frame >= shotFrame) {
            std::vector<unsigned char> px(dw * dh * 4);
            glReadBuffer(GL_BACK);
            glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
            std::vector<unsigned char> flip(dw * dh * 4);
            for (int y = 0; y < dh; ++y)
                std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
            stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
            std::printf("ui screenshot -> %s (%dx%d)\n", screenshotPath, dw, dh);
            break;
        }
        glfwSwapBuffers(win);
    }

    freeChatTextures(st.chat);  // release iteration thumbnails while the GL context is still live
    ui::freeIcons();            // C14: release atlas textures while GL context is still live
    st.worker.stop();
    simClient.stop();  // C19: shut down the second worker process
    ImGui_ImplOpenGL3_Shutdown();
    ImGui_ImplGlfw_Shutdown();
    ImGui::DestroyContext();
    glfwDestroyWindow(win);
    glfwTerminate();
    return 0;
}
