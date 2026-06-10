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
#include "ui/legacy_chat.h"
#include "ui/settings_modal.h"
#include "ui/status_bar.h"
#include "ui/title_bar.h"
#include "ui/viewport_panel.h"

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
    Tex logoIcon = loadTexture(assets + "\\logo_icon.png");

    std::string rerr;
    Renderer renderer;
    renderer.init(960, 720, rerr);
    // Viewport gradient driven by palette roles so it switches with the theme.
    renderer.setBackground(ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                           ui::palette.viewportTop[2], ui::palette.viewportBottom[0],
                           ui::palette.viewportBottom[1], ui::palette.viewportBottom[2]);

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
    st.logoIcon = logoIcon;
    st.renderer = &renderer;
    st.hitLayout = &hitLayout;
    st.idPass = &idPass;
    st.overlays = &overlays;

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

    const char* shotMode = std::getenv("AI_CAD_SHOT");  // screenshot-only: settings|chat|assembly|plan
    st.wantSettings = shot && shotMode && std::strcmp(shotMode, "settings") == 0;
    st.wantChat = shot && shotMode && std::strcmp(shotMode, "chat") == 0;
    st.wantAssembly = shot && shotMode && std::strcmp(shotMode, "assembly") == 0;
    st.wantPlan = shot && shotMode && std::strcmp(shotMode, "plan") == 0;
    st.wantPick = shot && shotMode && std::strcmp(shotMode, "pick") == 0;
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

        bool busy = st.job.running.load();

        ImGui_ImplOpenGL3_NewFrame();
        ImGui_ImplGlfw_NewFrame();
        ImGui::NewFrame();

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

        // ---------- dockspace host (between the title bar and the status bar) ----------
        const float statusH = 30.0f * scale;
        ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + barH));
        ImGui::SetNextWindowSize(ImVec2(vp->Size.x, vp->Size.y - barH - statusH));
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
            buildDefaultLayout(dockId, ImVec2(vp->Size.x, vp->Size.y - barH - statusH));
            st.layoutInit = true;
            st.resetLayout = false;
        }
        ImGui::DockSpace(dockId, ImVec2(0, 0), ImGuiDockNodeFlags_None);
        ImGui::End();

        // ---------- status bar ----------
        ui::drawStatusBar(st, vp, statusH, busy, hostFlags);

        bool canRun = st.workerStarted && !busy;

        // ---------- panels ----------
        ui::drawDesignPanel(st, busy, canRun);
        ui::drawViewportPanel(st, io, busy);
        ui::drawConsolePanel(st);

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

        if (shot && ++st.frame >= 6) {
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
    st.worker.stop();
    ImGui_ImplOpenGL3_Shutdown();
    ImGui_ImplGlfw_Shutdown();
    ImGui::DestroyContext();
    glfwDestroyWindow(win);
    glfwTerminate();
    return 0;
}
