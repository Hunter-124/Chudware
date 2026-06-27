#include "app/app_state.h"
#include "platform/file_dialog.h"
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
    const cmd::Command* c = cmd::find(cmdId);
    std::string label = c ? c->label : cmdId;

    if (std::strcmp(cmdId, "sketch_new") == 0) {
        inSketch = true; activeTool.clear();
        status = "Editing Sketch1"; return;
    }
    if (std::strcmp(cmdId, "sketch_exit") == 0) {
        inSketch = false; status = "Ready"; return;
    }
    if (std::strcmp(cmdId, "make_drawing") == 0) {
        docs.push_back({"Drawing1", cmd::DocType::Drawing});
        activeDoc = (int)docs.size() - 1; workspace = cmd::Workspace::Drafting;
        status = "Created Drawing1"; return;
    }
    if (std::strcmp(cmdId, "ecad_new_sch") == 0) {
        docs.push_back({"Schematic1", cmd::DocType::Schematic});
        activeDoc = (int)docs.size() - 1; workspace = cmd::Workspace::Schematic;
        status = "Created Schematic1"; return;
    }
    if (std::strcmp(cmdId, "ecad_new_pcb") == 0) {
        docs.push_back({"Board1", cmd::DocType::Pcb});
        activeDoc = (int)docs.size() - 1; workspace = cmd::Workspace::Board;
        status = "Created Board1"; return;
    }
    if (std::strcmp(cmdId, "ecad_ai_design") == 0) {
        startDemoAgent("Design a 5V regulator schematic.");
        status = "AI Design"; return;
    }

    if (opensInspector(cmdId)) {
        activeTool = cmdId;
        if (cmdId == std::string("extrude_boss") && toolExtrudeOp.empty()) toolExtrudeOp = "New Body";
        status = std::string("Editing ") + label;
        return;
    }
    status = std::string("Command: ") + label;
}

// Map an engine feature type -> an IconId for the tree.
static std::string featureIcon(const std::string& type) {
    if (type == "box") return "prim_box";
    if (type == "cylinder") return "prim_cylinder";
    if (type == "sphere") return "prim_sphere";
    if (type == "cone") return "prim_cone";
    if (type == "extrude") return "extrude";
    if (type == "revolve") return "revolve";
    if (type == "sweep") return "sweep";
    if (type == "loft") return "loft";
    if (type == "fillet") return "fillet";
    if (type == "chamfer") return "chamfer";
    if (type == "shell") return "shell";
    if (type == "draft") return "draft";
    if (type == "rib") return "rib";
    if (type == "hole" || type == "bolt_circle") return "hole";
    if (type == "sketch") return "ws_sketch";
    if (type == "helix") return "coil";
    if (type == "split") return "split";
    if (type == "boolean") return "combine";
    if (type == "linear_pattern") return "pattern_linear";
    if (type == "circular_pattern") return "pattern_circular";
    if (type == "mirror") return "mirror";
    if (type == "transform") return "move_component";
    return "extrude";
}

void AppState::buildTreeFromDoc() {
    tree = TreeNode{};
    tree.name = docs.empty() ? "Part" : doc().name;
    tree.icon = "file_part"; tree.isFolder = true; tree.open = true;
    TreeNode planes{ "Planes", "ref_plane", true, false,
        { {"Front", "ref_plane"}, {"Top", "ref_plane"}, {"Right", "ref_plane"} } };
    TreeNode origin{ "Origin", "ref_csys", false, true, {} };
    tree.children = { planes, origin };

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
    if (r.ok) { buildTreeFromDoc(); status = tool + " applied  (rev " + std::to_string(engine.revision()) + ")"; return true; }
    status = std::string("Engine error: ") + (r.errMsg.empty() ? engine.lastError() : r.errMsg);
    return false;
}

void AppState::doNewPart() {
    if (!engineReady) { status = "Engine offline"; return; }
    if (engine.newPart("Part")) {
        savePath.clear();
        if (!docs.empty()) docs[activeDoc].name = "Part";
        buildTreeFromDoc(); viewMeshRev = -1; activeTool.clear();
        status = "New part";
    } else status = std::string("New failed: ") + engine.lastError();
}

void AppState::doSave(bool saveAs) {
    if (!engineReady) { status = "Engine offline - nothing to save"; return; }
    std::string path = (saveAs || savePath.empty())
        ? platform::saveCadFile((docs.empty() ? std::string("Part1") : doc().name) + ".step+")
        : savePath;
    if (path.empty()) return;  // cancelled
    if (engine.save(path)) {
        savePath = path;
        if (!docs.empty()) docs[activeDoc].name = stemOf(path);
        status = "Saved " + path;
    } else status = std::string("Save failed: ") + engine.lastError();
}

void AppState::doOpen() {
    if (!engineReady) { status = "Engine offline"; return; }
    std::string path = platform::openCadFile();
    if (path.empty()) return;  // cancelled
    if (engine.open(path)) {
        savePath = path;
        if (!docs.empty()) { docs[activeDoc].name = stemOf(path); docs[activeDoc].type = cmd::DocType::Part; }
        workspace = cmd::Workspace::Model;
        buildTreeFromDoc(); viewMeshRev = -1; activeTool.clear();
        status = "Opened " + path;
    } else status = std::string("Open failed: ") + engine.lastError();
}

void AppState::seedDemo() {
    docs = { {"Part1", cmd::DocType::Part} };
    activeDoc = 0;
    workspace = cmd::Workspace::Model;

    // welcome agent message
    agent.blocks.clear();
    AgentBlock w; w.kind = BlockKind::AssistantMsg; w.anim = 1.f;
    w.title = "Assistant";
    w.body  = "Hi - I'm your CAD copilot. Ask me to model something, run a simulation, or "
              "explain a feature. You'll see my thinking and every action I take, inline.";
    agent.blocks.push_back(w);

    // --- Try the real C++ kernel (libaicad.dll): new part + a demo body. ---
    engineReady = engine.available();
    if (engineReady && engine.newPart("Part1")) {
        auto r1 = engine.addBox(40, 30, 15, "Base");
        if (r1.ok) { engine.addFilletAll(3, "Fillet"); engine.addShell(2, "Shell"); }
        engine.rebuild();              // produce an initial tess.aicm_path for the viewport phase
        buildTreeFromDoc();
        bodies = { "Base" };
        status = std::string("Engine live  (libaicad, rev ") + std::to_string(engine.revision()) + ")";
        return;
    }

    // --- Fallback: demo data (engine/DLL unavailable). ---
    engineReady = false;
    status = std::string("Demo mode - ") + (engine.lastError().empty() ? "engine unavailable" : engine.lastError());
    tree.name = "Part1"; tree.icon = "file_part"; tree.isFolder = true; tree.open = true;
    TreeNode origin{ "Origin", "ref_csys", false, true, {} };
    TreeNode planes{ "Planes", "ref_plane", true, false, {
        {"Front", "ref_plane"}, {"Top", "ref_plane"}, {"Right", "ref_plane"} } };
    TreeNode f1{ "Base", "extrude", false, true, {} };
    TreeNode f2{ "Fillet", "fillet", false, true, {} };
    TreeNode f3{ "Shell", "shell", false, true, {} };
    tree.children = { planes, origin, f1, f2, f3 };
    bodies = { "Part1<1>" };
}

// ---------------------------------------------------------------------------
//  Demo scripted agent — illustrates the transparent timeline (DESIGN.md §5).
//  Replaced by the real agent_run_v2 / events.jsonl stream in a later phase.
// ---------------------------------------------------------------------------
void AppState::startDemoAgent(const std::string& userText) {
    AgentBlock u; u.kind = BlockKind::UserMsg; u.body = userText; u.anim = 0.f;
    agent.blocks.push_back(u);
    agent.running = true;
    agent.demoStep = 0;
    agent.demoTimer = 0.f;
}

void AppState::tickDemoAgent(float dt) {
    // entrance animation for every block
    for (auto& b : agent.blocks) {
        if (b.anim < 1.f) b.anim += dt / 0.22f;
        if (b.anim > 1.f) b.anim = 1.f;
        if (b.kind == BlockKind::Action && b.status == ActionStatus::Running) b.spin += dt * 6.f;
    }
    if (!agent.running || agent.demoStep < 0) return;
    agent.demoTimer += dt;

    auto append = [&](AgentBlock b) { agent.blocks.push_back(b); };

    switch (agent.demoStep) {
        case 0: {  // thinking
            AgentBlock t; t.kind = BlockKind::Thinking; t.title = "Thinking";
            t.body = "The board needs mounting points. I'll drill four M3 clearance holes on a "
                     "30 mm bolt circle on the bottom face, then I can route the power net.";
            append(t);
            agent.demoStep = 1; agent.demoTimer = 0.f;
        } break;
        case 1: if (agent.demoTimer > 0.9f) {  // action: APPLY A REAL ENGINE OP
            AgentBlock a; a.kind = BlockKind::Action; a.title = "Bolt circle - 4x M3 on 30 mm PCD";
            a.args = "{\n  \"op\": \"add_feature\",\n  \"feature\": {\n    \"type\": \"bolt_circle\",\n"
                     "    \"params\": { \"diameter\": 3.4, \"count\": 4, \"pcd\": 30 },\n"
                     "    \"refs\": { \"face\": { \"of\": \"face\", \"side\": \"bottom\" } }\n  }\n}";
            a.status = ActionStatus::Running;
            append(a);
            // drive the real kernel
            if (engineReady) {
                auto r = engine.addFeature(nlohmann::json{
                    {"type","bolt_circle"},{"name","MountHoles"},
                    {"params",{{"diameter",3.4},{"count",4},{"pcd",30}}},
                    {"refs",{{"face",{{"of","face"},{"side","bottom"}}}}}});
                agent.demoOpOk = r.ok;
                agent.demoOpMsg = r.ok ? ("Added MountHoles (4 holes). Rebuilt to rev " + std::to_string(engine.revision()) + ".")
                                       : ("Engine: " + (r.errMsg.empty() ? engine.lastError() : r.errMsg));
                if (r.ok) buildTreeFromDoc();
            } else {
                agent.demoOpOk = true;
                agent.demoOpMsg = "Added 4 mounting holes (demo).";
            }
            agent.demoStep = 2; agent.demoTimer = 0.f;
        } break;
        case 2: if (agent.demoTimer > 1.1f) {  // resolve the action card from the real result
            for (auto it = agent.blocks.rbegin(); it != agent.blocks.rend(); ++it)
                if (it->kind == BlockKind::Action) {
                    it->status = agent.demoOpOk ? ActionStatus::Ok : ActionStatus::Failed;
                    it->result = agent.demoOpMsg;
                    break;
                }
            agent.demoStep = 3; agent.demoTimer = 0.f;
        } break;
        case 3: if (agent.demoTimer > 0.5f) {  // assistant summary
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
