#include "app/app_state.h"
#include "platform/file_dialog.h"
#include "viewport/scene.h"
#include "ui/ecad/ecad_panel.h"
#include <cstring>
#include <string>

namespace app {

static std::string stemOf(const std::string& path) {
    size_t s = path.find_last_of("/\\");
    std::string f = (s == std::string::npos) ? path : path.substr(s+1);
    size_t d = f.find_last_of('.');
    if (d != std::string::npos && d > 0) f = f.substr(0, d);
    return f.empty() ? "Part" : f;
}

static cmd::DocType tabKindToCmd(core::DocTabKind k) {
    switch (k) {
        case core::DocTabKind::Assembly: return cmd::DocType::Assembly;
        case core::DocTabKind::Drawing: return cmd::DocType::Drawing;
        case core::DocTabKind::Schematic: return cmd::DocType::Schematic;
        case core::DocTabKind::Pcb: return cmd::DocType::Pcb;
        default: return cmd::DocType::Part;
    }
}

static core::DocTabKind cmdToTabKind(cmd::DocType t) {
    switch (t) {
        case cmd::DocType::Assembly: return core::DocTabKind::Assembly;
        case cmd::DocType::Drawing: return core::DocTabKind::Drawing;
        case cmd::DocType::Schematic: return core::DocTabKind::Schematic;
        case cmd::DocType::Pcb: return core::DocTabKind::Pcb;
        default: return core::DocTabKind::Part;
    }
}

bool AppState::tryStartWorker() {
    if (workerStarted) return true;
    if (!worker.start(workerErr)) {
        // Python worker may be absent on dev Linux; cpp_core path still works for docs.
        engineReady = engine.available();
        return engineReady;
    }
    workerStarted = true;
    engineReady = engine.available();
    return engineReady;
}

void AppState::syncDocsFromTabs() {
    docs.clear();
    for (const auto& t : docTabs) {
        DocTabState d;
        d.name = t.displayTitle();
        d.type = tabKindToCmd(t.kind);
        docs.push_back(d);
    }
    if (activeTab >= 0 && activeTab < (int)docTabs.size()) activeDoc = activeTab;
    else if (!docs.empty()) activeDoc = 0;
}

void AppState::syncViewModel() {
    viewModel.tabs.clear();
    for (const auto& t : docTabs) {
        aicad::ui::DocTabView v;
        v.id = t.docId;
        v.title = t.displayTitle();
        v.dirty = t.dirty;
        switch (t.kind) {
            case core::DocTabKind::Assembly: v.kind = aicad::ui::DocKind::Assembly; break;
            case core::DocTabKind::Drawing: v.kind = aicad::ui::DocKind::Drawing; break;
            case core::DocTabKind::Schematic: v.kind = aicad::ui::DocKind::Schematic; break;
            case core::DocTabKind::Pcb: v.kind = aicad::ui::DocKind::Pcb; break;
            default: v.kind = aicad::ui::DocKind::Part; break;
        }
        viewModel.tabs.push_back(v);
    }
    viewModel.activeTab = activeTab >= 0 ? activeTab : 0;
    viewModel.docId = docMirror.docId();
    viewModel.revision = docMirror.revision();
    viewModel.statusMessage = status;
    viewModel.rebuildProgress = rebuildPct < 0.f ? 1.f : rebuildPct;
    viewModel.workspace.docKind = docs.empty() ? aicad::ui::DocKind::Part
        : (docs[activeDoc].type == cmd::DocType::Assembly ? aicad::ui::DocKind::Assembly
                                                          : aicad::ui::DocKind::Part);
    viewModel.workspace.active = inSketch ? aicad::ui::WorkspaceMode::Sketch
                                          : aicad::ui::WorkspaceMode::Model;
    selCount = (int)selection.size();
    viewModel.selection.summary = selCount > 0 ? (std::to_string(selCount) + " selected") : "";
}

void AppState::onDocMutation() {
    if (!suppressTabDirty && activeTab >= 0 && activeTab < (int)docTabs.size())
        docTabs[activeTab].dirty = true;
    buildTreeFromDoc();
    syncViewModel();
}

void AppState::addDocTab(bool isAssembly) {
    core::DocTab t;
    t.kind = isAssembly ? core::DocTabKind::Assembly : core::DocTabKind::Part;
    t.isAssembly = isAssembly;
    t.docId = docMirror.docId();
    t.title = isAssembly ? "Assembly1" : "Part1";
    docTabs.push_back(t);
    activeTab = (int)docTabs.size() - 1;
    syncDocsFromTabs();
    syncViewModel();
}

void AppState::switchToTab(int idx) {
    if (idx < 0 || idx >= (int)docTabs.size()) return;
    if (idx == activeTab) return;
    suppressTabDirty = true;
    std::string aicm, err;
    bool usedCache = false;
    const auto& target = docTabs[idx];
    if (!target.docId.empty()) {
        if (!docMirror.activateCached(worker, target.docId, target.session.cachedRevision,
                                      target.session.cachedAicmPath, aicm, usedCache, err))
            docMirror.activate(worker, target.docId, aicm, err);
    }
    activeTab = idx;
    activeDoc = idx;
    suppressTabDirty = false;
    buildTreeFromDoc();
    if (scene && !aicm.empty()) scene->loadAicm(aicm);
    syncDocsFromTabs();
    syncViewModel();
}

static bool opensInspector(const char* id) {
    static const char* k[] = {
        "prim_box","prim_cylinder","prim_sphere","prim_cone",
        "extrude_boss","revolve_boss","sweep_boss","loft_boss","rib","hole_wizard","curve_helix",
        "fillet","chamfer","shell","draft","split","boolean_combine",
        "linear_pattern","circular_pattern","mirror_feature",
        "ref_plane","ref_axis","ref_point","ref_csys",
        "sm_base_flange","sm_edge_flange","sm_hem","sm_jog",
        "weld_member","weld_gusset","weld_end_cap",
        "sim_new_static","sim_material","sim_fixture","sim_load_force","sim_mesh",
        "asm_insert","asm_mate","asm_pattern",
    };
    for (const char* s : k) if (std::strcmp(s, id) == 0) return true;
    return false;
}

void AppState::dispatch(const char* cmdId) {
    if (!cmdId || !*cmdId) return;

    aicad::ui::Action action;
    action.cmdId = cmdId;
    action.source = aicad::ui::ActionSource::Toolbar;
    auto diff = actionBus.submit(action);
    if (diff.ok) {
        onDocMutation();
        status = std::string("Command: ") + cmdId;
        return;
    }

    const cmd::Command* c = cmd::find(cmdId);
    std::string label = c ? c->label : cmdId;

    if (std::strcmp(cmdId, "sketch_new") == 0) {
        inSketch = true; activeTool.clear();
        status = "Editing Sketch1"; syncViewModel(); return;
    }
    if (std::strcmp(cmdId, "sketch_exit") == 0) {
        inSketch = false; status = "Ready"; syncViewModel(); return;
    }
    if (std::strcmp(cmdId, "make_drawing") == 0) {
        docs.push_back({"Drawing1", cmd::DocType::Drawing});
        activeDoc = (int)docs.size() - 1; workspace = cmd::Workspace::Drafting;
        status = "Created Drawing1"; syncViewModel(); return;
    }
    if (std::strcmp(cmdId, "ecad_new_sch") == 0) {
        docs.push_back({"Schematic1", cmd::DocType::Schematic});
        activeDoc = (int)docs.size() - 1; workspace = cmd::Workspace::Schematic;
        // H2: create the real worker ecad doc + load it into the ECAD panel's editor mirror (the
        // demo doc-tab bookkeeping above is unchanged; this is the dispatch-site delegate into
        // ui/ecad/** — see ecad_panel.h::ecadHandleNewDoc).
        if (workerStarted) ui::ecadHandleNewDoc(*this, "sch");
        status = "Created Schematic1"; syncViewModel(); return;
    }
    if (std::strcmp(cmdId, "ecad_new_pcb") == 0) {
        docs.push_back({"Board1", cmd::DocType::Pcb});
        activeDoc = (int)docs.size() - 1; workspace = cmd::Workspace::Board;
        if (workerStarted) ui::ecadHandleNewDoc(*this, "pcb");
        status = "Created Board1"; syncViewModel(); return;
    }
    if (std::strcmp(cmdId, "ecad_ai_design") == 0) {
        startDemoAgent("Design a 5V regulator schematic.");
        status = "AI Design"; return;
    }

    if (std::strcmp(cmdId, "view_style") == 0) {
        viewStyle = (viewStyle + 1) % 3;
        if (scene) scene->setStyle(viewStyle);
        static const char* nm[3] = {"Shaded", "Shaded + Edges", "Wireframe"};
        status = std::string("Display: ") + nm[viewStyle]; return;
    }
    if (std::strcmp(cmdId, "view_style_shaded") == 0) { viewStyle = 0; if (scene) scene->setStyle(0); status = "Display: Shaded"; return; }
    if (std::strcmp(cmdId, "view_style_edges")  == 0) { viewStyle = 1; if (scene) scene->setStyle(1); status = "Display: Shaded + Edges"; return; }
    if (std::strcmp(cmdId, "view_style_wire")   == 0) { viewStyle = 2; if (scene) scene->setStyle(2); status = "Display: Wireframe"; return; }

    if (std::strcmp(cmdId, "appearance_edit") == 0) {
        activeTool = "appearance_edit";
        railTab = 1;
        apNeedsInit = true;
        apBody = 0;
        if (scene && !scene->selection().empty()) apBody = scene->selection().front().body_id;
        status = "Appearance"; return;
    }

    const char* toolId = cmdId;
    if (c && c->kind == cmd::Kind::Variant && c->parent) toolId = c->parent;

    if (opensInspector(toolId)) {
        activeTool = toolId;
        if (c && c->kind == cmd::Kind::Variant) toolExtrudeOp = label;
        else if (std::strcmp(toolId, "extrude_boss") == 0 && toolExtrudeOp.empty()) toolExtrudeOp = "New Body";
        status = std::string("Editing ") + label;
        viewModel.inspector.active = true;
        viewModel.inspector.toolId = toolId;
        viewModel.inspector.title = label;
        return;
    }
    status = std::string("Command: ") + label;
}

static std::string featureIcon(const std::string& type) {
    if (type == "sketch") return "sketch_node";
    if (type == "extrude") return "extrude_boss";
    if (type == "revolve") return "revolve_boss";
    if (type == "sweep") return "sweep";
    if (type == "loft") return "loft";
    if (type == "fillet") return "fillet";
    if (type == "chamfer") return "chamfer";
    if (type == "shell") return "shell";
    if (type == "draft") return "draft";
    if (type == "rib") return "rib";
    if (type == "hole" || type == "bolt_circle") return "hole_wizard";
    if (type == "helix") return "curve_helix";
    if (type == "split") return "split";
    if (type == "boolean") return "combine";
    if (type == "linear_pattern") return "linear_pattern";
    if (type == "circular_pattern") return "circular_pattern";
    if (type == "mirror") return "mirror";
    if (type == "box") return "box_feature";
    if (type == "cylinder") return "cylinder_feature";
    if (type == "sphere") return "sphere_feature";
    if (type == "cone") return "cone_feature";
    if (type == "plane" || type == "datum_plane") return "ref_plane";
    if (type == "axis" || type == "datum_axis") return "ref_axis";
    if (type == "point" || type == "datum_point") return "ref_point";
    if (type == "csys") return "ref_csys";
    return "part";
}

void AppState::buildTreeFromDoc() {
    if (engineReady) viewMeshRev = -1;
    tree = TreeNode{};
    tree.name = docs.empty() ? "Part" : doc().name;
    tree.icon = "file_part"; tree.isFolder = true; tree.open = true;
    TreeNode origin{ "Origin", "origin", true, true, {
        {"Front Plane", "ref_plane"}, {"Top Plane", "ref_plane"}, {"Right Plane", "ref_plane"} } };
    tree.children = { origin };

    const nlohmann::json& t = engine.tree();
    if (t.is_object() && t.contains("features") && t["features"].is_array()) {
        for (const auto& f : t["features"]) {
            if (!f.is_object()) continue;
            std::string type = f.value("type", std::string("feature"));
            std::string name = f.value("name", type);
            TreeNode n; n.name = name; n.icon = featureIcon(type); n.open = true;
            tree.children.push_back(n);
        }
    }
    syncViewModel();
}

bool AppState::commitTool(const std::string& tool, double v1, double v2) {
    (void)v2;
    if (!engineReady) { status = tool + " applied (demo)"; return true; }
    eng::OpsResult r;
    if (tool == "fillet")            r = engine.addFilletAll(v1);
    else if (tool == "chamfer")      r = engine.addChamferAll(v1);
    else if (tool == "shell")        r = engine.addShell(v1);
    else if (tool == "draft")        r = engine.addDraftSides(v1);
    else if (tool == "hole_wizard")  r = engine.addHoleCenter(v1);
    else { status = "That tool needs a sketch (sketch wiring is next)."; return false; }
    if (r.ok) { onDocMutation(); status = tool + " applied  (rev " + std::to_string(engine.revision()) + ")"; return true; }
    status = std::string("Engine error: ") + (r.errMsg.empty() ? engine.lastError() : r.errMsg);
    return false;
}

void AppState::doNewPart() {
    if (!engineReady) { status = "Engine offline"; return; }
    if (engine.newPart("Part")) {
        savePath.clear();
        inSketch = false;
        docTabs.clear();
        addDocTab(false);
        if (!docs.empty()) docs[activeDoc].name = "Part";
        buildTreeFromDoc(); activeTool.clear();
        status = "New part";
    } else status = std::string("New failed: ") + engine.lastError();
}

void AppState::doSave(bool saveAs) {
    if (!engineReady) { status = "Engine offline - nothing to save"; return; }
    std::string path = (saveAs || savePath.empty())
        ? platform::saveCadFile((docs.empty() ? std::string("Part1") : doc().name) + ".step+")
        : savePath;
    if (path.empty()) return;
    if (engine.save(path)) {
        savePath = path;
        if (!docs.empty()) docs[activeDoc].name = stemOf(path);
        if (activeTab >= 0 && activeTab < (int)docTabs.size()) {
            docTabs[activeTab].path = path;
            docTabs[activeTab].dirty = false;
        }
        syncDocsFromTabs();
        status = "Saved " + path;
    } else status = std::string("Save failed: ") + engine.lastError();
    syncViewModel();
}

void AppState::doOpen() {
    if (!engineReady) { status = "Engine offline"; return; }
    std::string path = platform::openCadFile();
    if (path.empty()) return;
    if (engine.open(path)) {
        savePath = path;
        inSketch = false;
        docTabs.clear();
        addDocTab(false);
        if (!docs.empty()) {
            docs[activeDoc].name = stemOf(path);
            docs[activeDoc].type = cmd::DocType::Part;
        }
        if (activeTab >= 0 && activeTab < (int)docTabs.size()) docTabs[activeTab].path = path;
        workspace = cmd::Workspace::Model; camActive = false;
        buildTreeFromDoc(); activeTool.clear();
        status = "Opened " + path;
    } else status = std::string("Open failed: ") + engine.lastError();
    syncViewModel();
}

void AppState::seedDemo() {
    docs = { {"Part1", cmd::DocType::Part} };
    activeDoc = 0;
    workspace = cmd::Workspace::Model; camActive = false;

    actionBus.setDispatcher(&commandDispatcher);

    docMirror.setListener([this]() { onDocMutation(); });

    agent.blocks.clear();
    AgentBlock w; w.kind = BlockKind::AssistantMsg; w.anim = 1.f;
    w.title = "Assistant";
    w.body  = "Hi - I'm your CAD copilot. Ask me to model something, run a simulation, or "
              "explain a feature. You'll see my thinking and every action I take, inline.";
    agent.blocks.push_back(w);

    tryStartWorker();

    if (engineReady && engine.newPart("Part1")) {
        docTabs.clear();
        addDocTab(false);
        auto r1 = engine.addBox(40, 30, 15, "Base");
        if (r1.ok) { engine.addFilletAll(3, "Fillet"); engine.addShell(2, "Shell"); }
        engine.rebuild();
        buildTreeFromDoc();
        bodies = { "Base" };
        status = std::string("Engine live  (WorkerClient, rev ") + std::to_string(engine.revision()) + ")";
        syncViewModel();
        return;
    }

    engineReady = false;
    status = std::string("Demo mode - ") + (engine.lastError().empty() ? "engine unavailable" : engine.lastError());
    tree.name = "Part1"; tree.icon = "file_part"; tree.isFolder = true; tree.open = true;
    TreeNode origin{ "Origin", "origin", true, true, {
        {"Front Plane", "ref_plane"}, {"Top Plane", "ref_plane"}, {"Right Plane", "ref_plane"} } };
    TreeNode f1{ "Base", "extrude_boss", false, true, {} };
    TreeNode f2{ "Fillet", "fillet", false, true, {} };
    TreeNode f3{ "Shell", "shell", false, true, {} };
    tree.children = { origin, f1, f2, f3 };
    bodies = { "Part1<1>" };
    syncViewModel();
}

void AppState::startDemoAgent(const std::string& userText) {
    AgentBlock u; u.kind = BlockKind::UserMsg; u.body = userText; u.anim = 0.f;
    agent.blocks.push_back(u);
    agent.running = true;

    if (engineReady && engine.hasDoc()) {
        agentIsReal = true;
        agent.demoStep = -1;
        engine.startAgent(userText);
        AgentBlock spinner; spinner.kind = BlockKind::Action;
        spinner.title = "Running copilot…";
        spinner.status = ActionStatus::Running;
        spinner.anim = 0.f;
        agent.blocks.push_back(spinner);
    } else {
        agentIsReal = false;
        agent.demoStep = 0;
        agent.demoTimer = 0.f;
    }
}

void AppState::tickDemoAgent(float dt) {
    for (auto& b : agent.blocks) {
        if (b.anim < 1.f) b.anim += dt / 0.22f;
        if (b.anim > 1.f) b.anim = 1.f;
        if (b.kind == BlockKind::Action && b.status == ActionStatus::Running) b.spin += dt * 6.f;
    }

    if (agent.running && agentIsReal && engine.agentDone()) {
        nlohmann::json result = engine.takeAgentResult();
        bool ok = !result.contains("ok") || result.value("ok", true);
        std::string errMsg = result.value("error", std::string());
        if (result.contains("status") && result.value("status", std::string()) == "failed")
            ok = false;

        for (auto it = agent.blocks.rbegin(); it != agent.blocks.rend(); ++it) {
            if (it->kind == BlockKind::Action && it->status == ActionStatus::Running) {
                it->status = ok ? ActionStatus::Ok : ActionStatus::Failed;
                it->result = ok ? "Done" : (errMsg.empty() ? "Agent run failed" : errMsg);
                break;
            }
        }

        if (result.contains("events") && result["events"].is_array()) {
            for (const auto& ev : result["events"]) {
                std::string kind = ev.value("kind", std::string());
                if (kind == "run_note") {
                    AgentBlock t; t.kind = BlockKind::Thinking; t.title = "Thinking";
                    t.body = ev.value("note", ev.dump(2)); t.anim = 0.f;
                    agent.blocks.push_back(t);
                }
            }
        }

        AgentBlock m; m.kind = BlockKind::AssistantMsg; m.anim = 0.f;
        if (ok) {
            int rev = result.value("revision", -1);
            m.body = "Done." + (rev >= 0 ? "  Model is at rev " + std::to_string(rev) + "." : "");
        } else {
            m.body = "The copilot run failed: " + (errMsg.empty() ? "unknown error" : errMsg);
        }
        agent.blocks.push_back(m);

        std::string engaged = result.value("engaged", std::string("agent"));
        int itok = result.value("input_tokens", 0);
        int otok = result.value("output_tokens", 0);
        // Store real token counts so the context bar can show live k-token usage.
        if (itok > 0) agent.lastInputTokens  = itok;
        if (otok > 0) agent.lastOutputTokens = otok;
        AgentBlock us; us.kind = BlockKind::Usage; us.anim = 0.f;
        us.body = engaged;
        if (itok > 0 || otok > 0)
            us.body += "  |  " + std::to_string(itok) + "→" + std::to_string(otok) + " tok";
        agent.blocks.push_back(us);

        if (ok) {
            auto r = engine.refreshTree();
            if (r.ok) { onDocMutation(); viewMeshRev = -1; }
        }

        agent.running = false;
        agentIsReal = false;
        return;
    }

    if (!agent.running || agent.demoStep < 0) return;
    agent.demoTimer += dt;

    auto append = [&](AgentBlock b) { agent.blocks.push_back(b); };

    switch (agent.demoStep) {
        case 0: {
            AgentBlock t; t.kind = BlockKind::Thinking; t.title = "Thinking";
            t.body = "The board needs mounting points. I'll drill four M3 clearance holes on a "
                     "30 mm bolt circle on the bottom face, then I can route the power net.";
            append(t);
            agent.demoStep = 1; agent.demoTimer = 0.f;
        } break;
        case 1: if (agent.demoTimer > 0.9f) {
            AgentBlock a; a.kind = BlockKind::Action; a.title = "Bolt circle - 4x M3 on 30 mm PCD";
            a.args = "{\n  \"op\": \"add_feature\",\n  \"feature\": {\n    \"type\": \"bolt_circle\",\n"
                     "    \"params\": { \"diameter\": 3.4, \"count\": 4, \"pcd\": 30 },\n"
                     "    \"refs\": { \"face\": { \"of\": \"face\", \"side\": \"bottom\" } }\n  }\n}";
            a.status = ActionStatus::Running;
            append(a);
            if (engineReady) {
                auto r = engine.addFeature(nlohmann::json{
                    {"type","bolt_circle"},{"name","MountHoles"},
                    {"params",{{"diameter",3.4},{"count",4},{"pcd",30}}},
                    {"refs",{{"face",{{"of","face"},{"side","bottom"}}}}}});
                agent.demoOpOk = r.ok;
                agent.demoOpMsg = r.ok ? ("Added MountHoles (4 holes). Rebuilt to rev " + std::to_string(engine.revision()) + ".")
                                       : ("Engine: " + (r.errMsg.empty() ? engine.lastError() : r.errMsg));
                if (r.ok) onDocMutation();
            } else {
                agent.demoOpOk = true;
                agent.demoOpMsg = "Added 4 mounting holes (demo).";
            }
            agent.demoStep = 2; agent.demoTimer = 0.f;
        } break;
        case 2: if (agent.demoTimer > 1.1f) {
            for (auto it = agent.blocks.rbegin(); it != agent.blocks.rend(); ++it)
                if (it->kind == BlockKind::Action) {
                    it->status = agent.demoOpOk ? ActionStatus::Ok : ActionStatus::Failed;
                    it->result = agent.demoOpMsg;
                    break;
                }
            agent.demoStep = 3; agent.demoTimer = 0.f;
        } break;
        case 3: if (agent.demoTimer > 0.5f) {
            AgentBlock m; m.kind = BlockKind::AssistantMsg; m.title = "Assistant";
            m.body = agent.demoOpOk
                ? "Done - I drilled the four mounting holes through the base. Next I can place the "
                  "regulator, the input/output caps, and route the power net. Want me to continue?"
                : "That op didn't apply cleanly - see the error above. I can try a different face or "
                  "hole pattern if you'd like.";
            append(m);
            AgentBlock us; us.kind = BlockKind::Usage; us.body = "Local demo  |  scripted  |  drives the real kernel";
            append(us);
            agent.running = false; agent.demoStep = -1;
        } break;
        default: break;
    }
}

} // namespace app
