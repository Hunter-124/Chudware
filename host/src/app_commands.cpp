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

#include <cstdint>
#include <unordered_map>

namespace {

struct CheckedDispatch {
    const AppState* state;
    const std::string* command;
};
thread_local const CheckedDispatch* currentDispatch = nullptr;

struct CheckedDispatchScope {
    CheckedDispatch current;
    const CheckedDispatch* previous;
    CheckedDispatchScope(const AppState& state, const std::string& command)
        : current{&state, &command}, previous(currentDispatch) {
        currentDispatch = &current;
    }
    ~CheckedDispatchScope() { currentDispatch = previous; }
};

// One bounded active-document cache. A mirror generation changes on tree/status adoption,
// including same-revision refreshes and snapshot restores, so neither a tab switch nor a
// rebuild can reuse facts from a different tree.
struct CommandDocFacts {
    const AppState* owner = nullptr;
    std::string docId;
    int revision = -1;
    uint64_t generation = 0;
    bool treeKnown = false;
    bool solidContent = false;
    bool base = false, bend = false, flat = false;
    bool baseUnknown = true, bendUnknown = true, flatUnknown = true;
};

const CommandDocFacts& commandDocFacts(const AppState& st) {
    static CommandDocFacts cache;
    const auto& mirror = st.docMirror;
    if (cache.owner == &st && cache.docId == mirror.docId() &&
        cache.revision == mirror.revision() && cache.generation == mirror.generation())
        return cache;

    cache = {};
    cache.owner = &st;
    cache.docId = mirror.docId();
    cache.revision = mirror.revision();
    cache.generation = mirror.generation();
    const auto& tree = mirror.tree();
    if (!tree.is_object()) return cache;

    const auto components = tree.find("components");
    cache.solidContent = components != tree.end() && components->is_array() && !components->empty();

    // Statuses are the compiler's evidence that a sheet-metal feature actually executed.
    // Missing statuses are tracked separately from known failures; they are not fabricated.
    std::unordered_map<std::string, bool> status;
    const auto& statuses = mirror.featureStatus();
    if (mirror.featureStatusCurrent() && statuses.is_array()) {
        status.reserve(statuses.size());
        for (const auto& row : statuses) {
            if (!row.is_object()) continue;
            const auto id = row.find("id");
            if (id == row.end() || !id->is_string()) continue;
            const auto ok = row.find("ok");
            if (row.value("rolled_back", false))
                status.emplace(id->get<std::string>(), false);
            else if (ok != row.end() && ok->is_boolean())
                status.emplace(id->get<std::string>(), ok->get<bool>());
        }
    }
    const auto features = tree.find("features");
    if (features == tree.end() || !features->is_array()) return cache;
    cache.treeKnown = true;
    cache.baseUnknown = cache.bendUnknown = cache.flatUnknown = false;
    const auto rollbackEntry = tree.find("rollback_after");
    const std::string rollback = rollbackEntry != tree.end() && rollbackEntry->is_string()
                                     ? rollbackEntry->get<std::string>() : std::string();
    bool pastRollback = false;
    for (const auto& feature : *features) {
        if (!feature.is_object()) continue;
        const std::string id = feature.value("id", std::string());
        const bool afterRollback = pastRollback;
        if (!rollback.empty() && id == rollback) pastRollback = true;
        if (feature.value("suppressed", false)) continue;
        const std::string type = feature.value("type", std::string());
        if (!type.empty() && type != "plane" && type != "datum_plane" && type != "ref_plane" &&
            type != "axis" && type != "datum_axis" && type != "ref_axis" &&
            type != "point" && type != "datum_point" && type != "ref_point" &&
            type != "csys" && type != "coordinate_system" && type != "ref_csys" &&
            type != "sketch" && type != "sketch3d")
            cache.solidContent = true;
        if (afterRollback) continue;
        const bool isBase = type == "sm_base_flange";
        const bool isBend = type == "sm_edge_flange" || type == "sm_hem" || type == "sm_jog";
        const bool isFlat = type == "sm_flat_pattern";
        if (!isBase && !isBend && !isFlat) continue;
        const auto found = status.find(id);
        if (found == status.end()) {
            cache.baseUnknown |= isBase;
            cache.bendUnknown |= isBend;
            cache.flatUnknown |= isFlat;
        } else if (found->second) {
            cache.base |= isBase;
            cache.bend |= isBend;
            cache.flat |= isFlat;
        }
    }
    return cache;
}

}  // namespace

bool ui::commandDispatchChecked(const AppState& st, const char* cmdId) {
    return currentDispatch && currentDispatch->state == &st && cmdId &&
           *currentDispatch->command == cmdId;
}

ui::CommandDocKind ui::activeMechanicalDocumentKind(const AppState& st) {
    if (st.activeTab < 0 || st.activeTab >= static_cast<int>(st.docTabs.size()))
        return CommandDocKind::None;
    const auto& tab = st.docTabs[st.activeTab];
    using TabKind = AppState::DocTab::TabKind;
    if ((tab.kind != TabKind::Part && tab.kind != TabKind::Assembly) ||
        tab.docId.empty() || tab.docId != st.docMirror.docId())
        return CommandDocKind::None;

    // Handoff-created assemblies can retain a legacy Part tab kind. Trust the schema
    // only after binding the active tab to its own mirror, never a background document.
    const auto& tree = st.docMirror.tree();
    const auto schema = tree.find("schema");
    if (schema != tree.end() && schema->is_string()) {
        if (*schema == "aicad.asm/1") return CommandDocKind::Assembly;
        if (*schema == "aicad.part/1") return CommandDocKind::Part;
    }
    return tab.kind == TabKind::Assembly ? CommandDocKind::Assembly : CommandDocKind::Part;
}

ui::CommandContext ui::commandContext(const AppState& st) {
    CommandContext c;
    c.workspace = static_cast<int>(st.activeWorkspace);
    c.inSimulation = st.inSimulation();
    c.kicadStatusKnown = st.ecadKicadStatusKnown;
    if (st.ecadKicadStatus.is_object()) {
        const auto& status = st.ecadKicadStatus;
        c.kicadAvailable = status.value("found", false);
        c.kicadSchematicAvailable = !status.value("eeschema", std::string()).empty();
        c.kicadPcbAvailable = !status.value("pcbnew", std::string()).empty();
        c.kicadProjectAvailable = !status.value("kicad", std::string()).empty();
    }
    const std::string activeEcadProject =
        st.activeTab >= 0 && st.activeTab < static_cast<int>(st.docTabs.size()) &&
        !st.docTabs[st.activeTab].ecadDocId.empty()
            ? st.docTabs[st.activeTab].ecadProjectPath : std::string();
    for (const auto& tab : st.docTabs) {
        if (tab.ecadDocId.empty() || tab.ecadProjectPath != activeEcadProject) continue;
        c.hasOpenSchematic |= tab.kind == AppState::DocTab::TabKind::Schematic;
        c.hasOpenBoard |= tab.kind == AppState::DocTab::TabKind::PCB;
    }
    if (st.activeTab >= 0 && st.activeTab < static_cast<int>(st.docTabs.size())) {
        const auto& tab = st.docTabs[st.activeTab];
        c.ecadHasPath = !tab.path.empty();
        c.ecadHasProject = !tab.ecadProjectPath.empty();
        switch (tab.kind) {
        case AppState::DocTab::TabKind::Part:
            c.activeDocKind = CommandDocKind::Part; c.activeDocId = tab.docId; break;
        case AppState::DocTab::TabKind::Assembly:
            c.activeDocKind = CommandDocKind::Assembly; c.activeDocId = tab.docId; break;
        case AppState::DocTab::TabKind::Drawing:
            c.activeDocKind = CommandDocKind::Drawing; c.activeDocId = tab.drawingDocId;
            c.drawingViewSelected = !tab.selViewId.empty(); break;
        case AppState::DocTab::TabKind::Schematic:
            c.activeDocKind = CommandDocKind::Schematic; c.activeDocId = tab.ecadDocId; break;
        case AppState::DocTab::TabKind::PCB:
            c.activeDocKind = CommandDocKind::PCB; c.activeDocId = tab.ecadDocId; break;
        }
    }
    const CommandDocKind mechanicalKind = activeMechanicalDocumentKind(st);
    const bool mechanical = mechanicalKind != CommandDocKind::None;
    c.activeMechanicalReady = mechanical;
    if (mechanical) {
        c.activeDocKind = mechanicalKind;
        c.activeDocRevision = st.docMirror.revision();
        if (st.sketchMode) {
            c.sketch2dActive = st.sketchMode->active();
            c.sketch2dHasEntitySelection = c.sketch2dHasFixSelection =
                st.sketchMode->hasEditableSelection();
        }
        if (st.sketch3dMode && st.sketch3dMode->documentId() == c.activeDocId) {
            c.sketch3dActive = st.sketch3dMode->active();
            c.sketch3dHasSelection = st.sketch3dMode->hasSelectedEntities();
            c.sketch3dHasFixSelection = st.sketch3dMode->hasFixableSelection();
        }
        const auto& facts = commandDocFacts(st);
        if (st.docMirror.solidPresenceKnown()) {
            c.modelSolidKnown = true;
            c.modelHasSolid = st.docMirror.hasSolidBody();
        } else {
            // An empty tree with no displayed bodies proves absence. Otherwise an older or
            // partially hydrated mirror has not established whether its content is solid.
            c.modelSolidKnown = facts.treeKnown && !facts.solidContent && st.docBodyIds.empty();
        }
        c.sheetMetalHasBase = facts.base;
        c.sheetMetalHasBends = facts.bend;
        c.sheetMetalHasValidFlat = facts.flat;
        c.sheetMetalBaseStatusUnknown = facts.baseUnknown;
        c.sheetMetalBendStatusUnknown = facts.bendUnknown;
        c.sheetMetalFlatStatusUnknown = facts.flatUnknown;
    }
    if (st.simManager) st.simManager->populateCommandContext(c);
    camPopulateCommandContext(st, c);
    return c;
}

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


bool CommandRouter::dispatch(AppState& st, const char* cmdId) const {
    return dispatch(st, cmdId, ui::commandContext(st));
}

bool CommandRouter::dispatch(AppState& st, const char* cmdId,
                             const ui::CommandContext& context) const {
    const ui::CommandAvailability availability = ui::commandAvailability(cmdId, context);
    if (!availability.visible || !availability.enabled) {
        if (availability.reason) st.toast(ui::ToastLevel::Info, availability.reason);
        return true;  // consumed without invoking any handlers
    }
    const std::string cmd = cmdId ? cmdId : "";
    // A PM and a new-sketch plane pick must never remain active simultaneously.
    if (cmd == "sketch_new" && st.propertyManager && st.propertyManager->active() &&
        !st.propertyManager->cancelEdits(st))
        return true;
    const CheckedDispatchScope checked(st, cmd);
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

    // ECAD commands (ecad_/sch_/pcb_) route to the ECAD editor. Always consumed.
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
