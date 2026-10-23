// app_commands.cpp — ribbon command table construction.
//
// Each family that used to be an inline block in app.cpp's dispatchCmd lambda is now a rule in an
// ordered table. Workspace families route to the owning module's existing entry point
// (SketchMode / Sketch3DMode::onRibbonCommand, drawingHandleCommand, AsmPanels::handleCommand,
// ecadDispatch, camHandleCommand, reveng::handleRevEngCommand, PropertyManager::openForCommand,
// SimManager / EvaluateDialogs::handleCommand). App-level families (Edit-menu clipboard verbs,
// Make Drawing, Display style, Appearance presets, Sheet Metal one-clicks) are implemented in
// app.cpp and arrive via RibbonCommandHooks.
//
// REGISTRATION ORDER IS THE SEMANTICS. buildRibbonCommandRouter registers rules in exactly the
// order the original if-chain tested them, and CommandRouter::dispatch stops at the first rule
// that matches AND consumes (returns true). Rules that may decline a command (dwg_, asm_,
// reveng_, sim_, eval_, the sketch hooks, the PM form opener) fall through to later rules just
// like the original `if (prefix && handler(...)) return;` blocks did.
#include "app_commands.h"
#include <string_view>

#include "core/app_state.h"
#include "ui/asm_panels.h"               // C20b assembly panels
#include "ui/cam/cam_panel.h"            // camHandleCommand
#include "ui/drawing_panel.h"            // drawingHandleCommand
#include "ui/evaluate_dialogs.h"         // C19 Evaluate tab dialogs
#include "ui/property_manager.h"         // PropertyManager::openForCommand
#include "ui/reveng/reveng_internal.h"   // reveng::handleRevEngCommand
#include "ui/sim_manager.h"              // C19 Simulation panel
#include "ui/sketch3d_mode.h"            // 3D free-space sketcher
#include "ui/sketch_mode.h"              // C18 sketch mode
#include "ui/ribbon_def.h"

// ---------------------------------------------------------------------------
// CommandRouter
// ---------------------------------------------------------------------------

void CommandRouter::registerExact(const char* id, Handler h) {
    rules_.push_back({Kind::Exact, id, std::move(h)});
}

void CommandRouter::registerPrefix(const char* prefix, Handler h) {
    rules_.push_back({Kind::Prefix, prefix, std::move(h)});
}

void CommandRouter::registerAny(Handler h) {
    rules_.push_back({Kind::Any, std::string(), std::move(h)});
}

bool CommandRouter::authorize(AppState& st, const char* cmdId) const {
    if (ui::ribbonCommandCapability(cmdId) != ui::CommandCapability::Mutate) return true;
    const std::string_view cmd = cmdId ? cmdId : "";
    if (st.activeDocLockedByAgent()) {
        // Cancel is routed to the session's keep-exit fallback while an agent owns the doc.
        if (cmd == "sketch_exit" || cmd == "sketch3d_exit" ||
            cmd == "sketch_cancel" || cmd == "sketch3d_cancel") return true;
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return false;
    }
    return true;
}

bool CommandRouter::dispatch(AppState& st, const char* cmdId) const {
    const std::string cmd = cmdId ? cmdId : "";
    for (const Rule& rule : rules_) {
        bool match = false;
        switch (rule.kind) {
            case Kind::Exact:  match = (cmd == rule.key); break;
            case Kind::Prefix: match = cmd.rfind(rule.key, 0) == 0; break;
            case Kind::Any:    match = true; break;
        }
        if (match && rule.handler(st, cmd)) return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// Table construction — registration order mirrors the original dispatchCmd if-chain.
// ---------------------------------------------------------------------------

CommandRouter buildRibbonCommandRouter(const RibbonCommandHooks& hooks) {
    CommandRouter r;

    // C18: sketch-tab commands (sketch_new / sk_* / smart_dim / sketch_exit) go to SketchMode
    // first so the sketch experience takes priority over any PM form for those ids.
    r.registerAny([](AppState& st, const std::string& cmd) {
        return st.sketchMode->onRibbonCommand(st, cmd);
    });
    // 3D sketch entry point + exit (all in-mode tools live on the viewport overlay toolbar).
    r.registerAny([](AppState& st, const std::string& cmd) {
        return st.sketch3dMode && st.sketch3dMode->onRibbonCommand(st, cmd);
    });

    // Wave 4C/4D + Wave-5-fill: Edit-menu clipboard verbs. Routed AFTER the sketch hook so an
    // armed sketch tool doesn't swallow them — these ids are never sketch-tool ids, so
    // onRibbonCommand returns false for them.
    r.registerExact("edit_copy",       hooks.editCopy);
    r.registerExact("edit_cut",        hooks.editCut);
    r.registerExact("edit_paste",      hooks.editPaste);
    r.registerExact("edit_delete",     hooks.editDelete);
    r.registerExact("edit_select_all", hooks.editSelectAll);

    r.registerExact("make_drawing", hooks.makeDrawing);

    // W17 drawing overhaul: View Layout / Annotation ribbon commands route to the 2D panel.
    // Falls through when the drawing panel declines the id.
    r.registerPrefix("dwg_", [](AppState& st, const std::string& cmd) {
        return ui::drawingHandleCommand(st, cmd);
    });

    // Appearance tab: Display group.
    r.registerExact("display_shaded",       hooks.displayStyle);
    r.registerExact("display_shaded_edges", hooks.displayStyle);
    r.registerExact("display_hlr",          hooks.displayStyle);
    r.registerExact("display_hlv",          hooks.displayStyle);
    r.registerExact("display_wireframe",    hooks.displayStyle);

    // Appearance tab: Material group.
    r.registerExact("appr_color",        hooks.appearance);
    r.registerExact("appr_clear",        hooks.appearance);
    r.registerExact("appr_mat_plastic",  hooks.appearance);
    r.registerExact("appr_mat_aluminum", hooks.appearance);
    r.registerExact("appr_mat_steel",    hooks.appearance);
    r.registerExact("appr_mat_brass",    hooks.appearance);
    r.registerExact("appr_mat_dark",     hooks.appearance);

    // C20b: Assembly tab + New Assembly route to the assembly panels first. Falls through when
    // the panels decline the id.
    auto asmRule = [](AppState& st, const std::string& cmd) {
        return st.asmPanels->handleCommand(st, cmd);
    };
    r.registerPrefix("asm_", asmRule);
    r.registerExact("new_assembly", asmRule);

    // Wave N: AI-ECAD commands (ecad_/sch_/pcb_) route to the ECAD editor. Always consumed.
    auto ecadRule = [hooks](AppState& st, const std::string& cmd) {
        st.showPcbTools = true;
        hooks.ecadDispatch(st, cmd);
        return true;
    };
    r.registerPrefix("ecad_", ecadRule);
    r.registerPrefix("sch_",  ecadRule);
    r.registerPrefix("pcb_",  ecadRule);

    // CAM (Manufacture workspace): cam_* commands route to the CAM manager panel. Always consumed.
    r.registerPrefix("cam_", [](AppState& st, const std::string& cmd) {
        ui::camHandleCommand(st, cmd);
        return true;
    });

    // Reverse Engineering workspace: reveng_* commands route to the RE workspace controller
    // (stage navigation + async kicks; contracts/reveng.md). Falls through when declined.
    r.registerPrefix("reveng_", [](AppState& st, const std::string& cmd) {
        return ui::reveng::handleRevEngCommand(st, cmd.c_str());
    });

    // PropertyManager forms: a command with a registered form opens it. Deliberately sits after
    // the prefix owners above and before the sm_/sim_/eval_ rules below, exactly like the
    // original chain (an sm_/sim_/eval_ id with a PM form opens the form first).
    r.registerAny([](AppState& st, const std::string& cmd) {
        return st.propertyManager->openForCommand(st, cmd);
    });

    // Sheet Metal one-click commands (no PM form).
    r.registerExact("sm_flat_pattern",    hooks.flatPattern);  // W16: one-click Flatten
    r.registerExact("sm_export_flat_dxf", hooks.flatDxf);      // W14: flat-pattern DXF

    // C19: Simulation tab -> sim panel. The tools column is revealed even when the panel
    // declines the id (falls through to the stub log, as before).
    r.registerPrefix("sim_", [](AppState& st, const std::string& cmd) {
        st.showSimulationTools = true;
        return st.simManager && st.simManager->handleCommand(st, cmd);
    });
    // C19: Evaluate tab -> evaluate dialogs. Falls through when declined.
    r.registerPrefix("eval_", [](AppState& st, const std::string& cmd) {
        return st.evaluateDialogs->handleCommand(st, cmd);
    });

    return r;
}
