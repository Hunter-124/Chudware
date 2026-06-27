#include "app/app_state.h"
#include <cstring>

namespace app {

static bool opensInspector(const char* id) {
    static const char* k[] = {
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

void AppState::seedDemo() {
    docs = { {"Bracket", cmd::DocType::Part} };
    activeDoc = 0;
    workspace = cmd::Workspace::Model;

    tree.name = "Bracket"; tree.icon = "file_part"; tree.isFolder = true; tree.open = true;
    TreeNode origin{ "Origin", "ref_csys", false, true, {} };
    TreeNode planes{ "Planes", "ref_plane", true, false, {
        {"Front", "ref_plane"}, {"Top", "ref_plane"}, {"Right", "ref_plane"} } };
    TreeNode s1{ "Sketch1", "ws_sketch", false, true, {} };
    TreeNode f1{ "Extrude1", "extrude", true, true, { {"Sketch1", "ws_sketch"} } };
    TreeNode f2{ "Fillet1", "fillet", false, true, {} };
    TreeNode f3{ "Shell1", "shell", false, true, {} };
    TreeNode f4{ "LPattern1", "pattern_linear", false, true, {} };
    tree.children = { planes, origin, f1, f2, f3, f4 };

    bodies = { "Bracket<1>" };

    // welcome agent message
    agent.blocks.clear();
    AgentBlock w; w.kind = BlockKind::AssistantMsg; w.anim = 1.f;
    w.title = "Assistant";
    w.body  = "Hi - I'm your CAD copilot. Ask me to model something, run a simulation, or "
              "explain a feature. You'll see my thinking and every action I take, inline.";
    agent.blocks.push_back(w);
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
            t.body = "The user wants a regulator. I'll start a sketch on the top plane, extrude a "
                     "base, then place the components. Let me create the base body first.";
            append(t);
            agent.demoStep = 1; agent.demoTimer = 0.f;
        } break;
        case 1: if (agent.demoTimer > 1.0f) {  // action: extrude (running)
            AgentBlock a; a.kind = BlockKind::Action; a.title = "Extrude - Base 40x30x3 mm";
            a.args = "{\n  \"op\": \"extrude_boss\",\n  \"depth\": 3.0,\n  \"profile\": \"Sketch1\"\n}";
            a.status = ActionStatus::Running;
            append(a);
            agent.demoStep = 2; agent.demoTimer = 0.f;
        } break;
        case 2: if (agent.demoTimer > 1.4f) {  // action resolves
            for (auto it = agent.blocks.rbegin(); it != agent.blocks.rend(); ++it)
                if (it->kind == BlockKind::Action) {
                    it->status = ActionStatus::Ok;
                    it->result = "Created body Bracket<1> (1 solid, watertight). +1 feature.";
                    break;
                }
            agent.demoStep = 3; agent.demoTimer = 0.f;
        } break;
        case 3: if (agent.demoTimer > 0.5f) {  // assistant summary
            AgentBlock m; m.kind = BlockKind::AssistantMsg; m.title = "Assistant";
            m.body = "Done - I created a 40x30x3 mm base body. Next I can place the regulator, "
                     "input/output caps, and route the power net. Want me to continue?";
            append(m);
            AgentBlock us; us.kind = BlockKind::Usage; us.body = "Claude  |  4.2s  |  1,840 tokens";
            append(us);
            agent.running = false; agent.demoStep = -1;
        } break;
        default: break;
    }
}

} // namespace app
