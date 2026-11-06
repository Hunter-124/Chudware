#include "ui/ribbon_def.h"

#include <iostream>
#include <string_view>
#include <vector>

// Classification and the shared, GL-free availability policy.
int main() {
    using ui::CommandCapability;
    int failures = 0;
    auto check = [&failures](bool condition, const char* label) {
        if (!condition) {
            std::cerr << "FAIL: " << label << '\n';
            ++failures;
        }
    };
    check(ui::ribbonCommandCapability("extrude_boss") == CommandCapability::Mutate,
          "edit command is mutating");
    check(ui::ribbonCommandCapability("new_command_added_later") == CommandCapability::Mutate,
          "unknown command fails closed");
    check(ui::ribbonCommandCapability("eval_measure") == CommandCapability::Inspect,
          "measure is inspect-only");
    check(ui::ribbonCommandCapability("display_wireframe") == CommandCapability::Inspect,
          "view is inspect-only");
    check(ui::ribbonCommandCapability("sm_export_flat_dxf") == CommandCapability::Export,
          "flat export is export");
    check(ui::ribbonCommandCapability("dwg_export_pdf") == CommandCapability::Export,
          "drawing export is export");

    using ui::CommandContext;
    using ui::CommandDocKind;
    struct Case {
        const char* label;
        const char* command;
        CommandContext context;
        bool visible;
        bool enabled;
    };
    std::vector<Case> cases;
    auto add = [&](const char* label, const char* command, const CommandContext& context,
                   bool visible, bool enabled) {
        cases.push_back({label, command, context, visible, enabled});
    };

    CommandContext part;
    part.activeDocKind = CommandDocKind::Part;
    part.activeDocId = "part-1";
    part.activeDocRevision = 1;
    part.studyFactsCurrent = true;
    add("new sketch collects a plane later", "sketch_new", part, true, true);
    add("inactive 2D tool", "sk_line", part, true, false);
    add("inactive 2D dimension", "smart_dim", part, true, false);
    add("inactive 2D finish", "sketch_exit", part, true, false);
    add("inactive 3D tool", "sk3d_line", part, true, false);
    add("feature form collects its references", "fillet", part, true, true);
    add("new 3D sketch", "sketch3d_new", part, true, true);

    CommandContext sketch2d = part;
    sketch2d.sketch2dActive = true;
    add("active 2D tool", "sk_line", sketch2d, true, true);
    add("no second sketch in a live edit", "sketch_new", sketch2d, true, false);
    add("no 3D sketch during 2D edit", "sketch3d_new", sketch2d, true, false);
    add("2D finish remains available", "sketch_exit", sketch2d, true, true);
    add("2D cancel remains available", "sketch_cancel", sketch2d, true, true);
    add("construction requires entities", "sk_construction", sketch2d, true, false);
    add("fix requires selection", "sk_fix", sketch2d, true, false);
    add("convert collects later picks", "sk_convert_ent", sketch2d, true, true);
    sketch2d.sketch2dHasEntitySelection = true;
    sketch2d.sketch2dHasFixSelection = true;
    add("construction with selected entities", "sk_construction", sketch2d, true, true);
    add("fix with selected entities", "sk_fix", sketch2d, true, true);

    CommandContext sketch3d = part;
    sketch3d.sketch3dActive = true;
    add("active 3D tool", "sk3d_line", sketch3d, true, true);
    add("3D finish", "sketch3d_exit", sketch3d, true, true);
    add("shared finish handles 3D", "sketch_exit", sketch3d, true, true);
    add("3D cancel", "sketch3d_cancel", sketch3d, true, true);
    add("3D construction is a tool toggle", "sk3d_construction", sketch3d, true, true);
    add("3D fix without a selection", "sk3d_fix", sketch3d, true, false);
    add("3D delete without entities", "sk3d_delete", sketch3d, true, false);
    sketch3d.sketch3dHasFixSelection = true;
    add("3D point can be fixed", "sk3d_fix", sketch3d, true, true);
    add("a point handle alone is not a deletable entity", "sk3d_delete", sketch3d, true, false);
    sketch3d.sketch3dHasSelection = true;
    add("3D selected entity can be deleted", "sk3d_delete", sketch3d, true, true);

    CommandContext drawing = part;
    drawing.activeDocKind = CommandDocKind::Drawing;
    drawing.activeDocId.clear();
    add("drawing tab requires a registry id", "dwg_note", drawing, true, false);
    drawing.activeDocId = "drawing-1";
    add("drawing annotation accepts a later pick", "dwg_note", drawing, true, true);
    add("drawing scale requires a selected view", "dwg_scale_1_1", drawing, true, false);
    add("background part is not an assembly", "asm_insert", drawing, true, false);
    add("background part cannot start a sketch", "sketch_new", drawing, true, false);
    drawing.drawingViewSelected = true;
    add("drawing scale after view selection", "dwg_scale_1_1", drawing, true, true);

    CommandContext assembly = part;
    assembly.activeDocKind = CommandDocKind::Assembly;
    assembly.activeDocId = "assembly-1";
    add("assembly operation on a part", "asm_mate", part, true, false);
    add("parts library is workspace navigation", "asm_parts_library", part, true, true);
    add("assembly operation collects components later", "asm_insert", assembly, true, true);
    add("motion needs an active assembly", "sim_new_motion", part, true, false);
    add("a background assembly does not enable motion on the active part",
        "sim_motion_study", part, true, false);
    add("motion on the active assembly", "sim_new_motion", assembly, true, true);
    CommandContext switching = part;
    switching.activeMechanicalReady = false;
    add("unbound tab cannot act on an old mirror", "sketch_new", switching, true, false);
    add("unbound tab cannot create a study on an old mirror", "sim_new_static", switching, true, false);
    add("drawing cannot create a study on a background part", "sim_new_static", drawing, true, false);
    add("drawing cannot persist a network in a background part", "sim_piping_new", drawing, true, false);
    add("drawing may open a nonmutating piping form", "sim_piping_flow", drawing, true, true);
    add("drawing cannot configure a study in a background part",
        "sim_modal_options", drawing, true, false);

    CommandContext sim = part;
    sim.inSimulation = true;
    add("simulation freezes reference geometry", "ref_plane", sim, true, false);
    add("empty selected study disables shared load", "sim_load_force", sim, true, false);
    add("no typed modal study", "sim_modal_options", sim, true, false);
    add("no typed thermal study", "sim_load_heat", sim, true, false);
    add("no typed topology study", "sim_topology_compare", sim, true, false);
    add("scoped buckling row is hidden", "sim_buckling_options", sim, false, false);
    add("parameters need a selected study", "sim_parameters", sim, true, false);
    sim.selectedStudyType = "static";
    sim.hasModalStudy = true;
    sim.hasThermalFeaStudy = true;
    sim.hasTopologyStudy = true;
    sim.hasThermalStudy = true;
    add("typed modal study may be in background", "sim_modal_options", sim, true, true);
    add("typed heat study may be in background", "sim_load_heat", sim, true, true);
    add("typed topology study may be in background", "sim_topology_compare", sim, true, true);
    add("thermal CFD supports flow setup", "sim_flow_bc", sim, true, true);
    add("parameters available for the selected study", "sim_parameters", sim, true, true);

    CommandContext stale = sim;
    stale.studyFactsCurrent = false;
    add("stale cached modal study cannot be targeted", "sim_modal_options", stale, true, false);
    add("stale selected study cannot be targeted", "sim_results", stale, true, false);
    add("stale parameters cannot target an old selection", "sim_parameters", stale, true, false);
    add("new study waits for current facts", "sim_new_modal", stale, true, false);
    add("picker remains available to retry", "sim_new_picker", stale, true, true);
    CommandContext missing = sim;
    missing.hasModalStudy = false;
    const auto staleModal = ui::commandAvailability("sim_modal_options", stale);
    const auto missingModal = ui::commandAvailability("sim_modal_options", missing);
    check(staleModal.reason && missingModal.reason &&
              std::string_view(staleModal.reason) != std::string_view(missingModal.reason),
          "unknown and confirmed-missing studies have distinct reasons");

    CommandContext loading = sim;
    loading.studyListPending = true;
    add("pending list blocks a previously known study", "sim_modal_options", loading, true, false);
    add("pending list blocks selected-study action", "sim_results", loading, true, false);
    add("pending list blocks parameters", "sim_parameters", loading, true, false);
    add("pending list blocks study creation", "sim_new_static", loading, true, false);
    add("pending list blocks industrial creation", "sim_piping_new", loading, true, false);
    add("pending list keeps picker browseable", "sim_new_picker", loading, true, true);
    add("pending list leaves EM setup available", "sim_em_ports", loading, true, true);
    add("pending list leaves nonmutating piping forms available", "sim_piping_flow", loading, true, true);

    CommandContext creating = sim;
    creating.simCreationPending = true;
    add("queued creation prevents a duplicate study", "sim_new_modal", creating, true, false);
    add("queued creation does not block existing setup", "sim_modal_options", creating, true, true);
    add("queued creation does not block existing parameters", "sim_parameters", creating, true, true);
    add("queued creation does not block the picker", "sim_new_picker", creating, true, true);
    CommandContext creatingAssembly = assembly;
    creatingAssembly.simCreationPending = true;
    add("queued creation prevents duplicate motion", "sim_new_motion", creatingAssembly, true, false);
    add("queued creation cannot open or create motion concurrently",
        "sim_motion_study", creatingAssembly, true, false);
    add("current list permits creation without unrelated metadata gating", "sim_new_modal", sim, true, true);
    add("modal scoped row still hidden in static", "sim_freqresp_options", sim, false, false);
    sim.selectedStudyType = "buckling";
    add("scoped row appears when selected", "sim_buckling_options", sim, true, true);
    sim.selectedStudyType = "topology";
    sim.topologyPhysics = "thermal";
    add("thermal topology takes no force", "sim_load_force", sim, true, false);
    add("thermal topology takes no pressure", "sim_load_pressure", sim, true, false);
    sim.topologyPhysics = "thermomechanical";
    add("thermomechanical topology accepts structural loads", "sim_load_force", sim, true, true);
    sim.topologyPhysics.clear();
    add("topology physics must be chosen for loads", "sim_load_force", sim, true, false);
    sim.modelHasSolid = false;
    add("topology needs a design domain", "sim_new_topology", sim, true, false);
    sim.modelSolidKnown = false;
    add("unknown solid state is not a fabricated absence", "sim_new_topology", sim, true, true);
    sim.modelSolidKnown = true;
    sim.modelHasSolid = true;
    add("topology on solid part", "sim_new_topology", sim, true, true);
    sim.topologyCapabilityKnown = true;
    sim.topologyCapabilityAvailable = false;
    add("known missing topology backend", "sim_new_topology", sim, true, false);
    add("EM run needs an analysis", "sim_em_run", sim, true, false);
    sim.emAnalysisOpen = true;
    add("EM run waits for capability", "sim_em_run", sim, true, false);
    sim.emCapabilityProbeFailed = true;
    add("EM run remains unavailable after a failed probe", "sim_em_run", sim, true, false);
    sim.emCapabilityProbeFailed = false;
    sim.emCapabilityKnown = true;
    sim.emCapabilityAvailable = false;
    add("EM run needs a compatible backend", "sim_em_run", sim, true, false);
    add("EM setup remains editable", "sim_em_ports", sim, true, true);
    sim.emCapabilityAvailable = true;
    add("EM run after capability succeeds", "sim_em_run", sim, true, true);
    sim.contactCapabilityKnown = true;
    sim.contactCapabilityAvailable = false;
    add("known missing contact solver", "sim_new_contact", sim, true, false);
    sim.vibrationCapabilityKnown = true;
    sim.vibrationCapabilityAvailable = false;
    add("known missing vibration solver", "sim_new_freqresp", sim, true, false);
    add("internal simulation navigation is retained", "sim_piping_new", sim, true, true);
    CommandContext frozenEdit = sketch2d;
    frozenEdit.inSimulation = true;
    add("live sketch can finish in simulation", "sketch_exit", frozenEdit, true, true);
    add("live sketch can cancel in simulation", "sketch_cancel", frozenEdit, true, true);

    CommandContext sch;
    sch.activeDocKind = CommandDocKind::Schematic;
    sch.activeDocId = "sch-1";
    sch.hasOpenSchematic = true;
    sch.kicadStatusKnown = true;
    sch.kicadAvailable = true;
    sch.kicadSchematicAvailable = true;
    sch.kicadPcbAvailable = true;
    sch.kicadProjectAvailable = true;
    sch.ecadHasPath = true;
    sch.ecadHasProject = true;
    int ribbonCount = 0;
    const auto* ribbon = ui::getRibbonCmds(ribbonCount);
    for (const char* command : {"ecad_ai_design", "ecad_open", "ecad_save", "ecad_run_erc",
         "ecad_run_drc", "pcb_update_from_sch", "ecad_autoroute", "ecad_fill_zones",
         "ecad_to_3d", "ecad_simulate", "ecad_export_bom", "sch_export_netlist",
         "sch_export_pdf", "ecad_new_project", "ecad_kicad_open_sch",
         "ecad_kicad_open_pcb", "ecad_kicad_open_project", "ecad_export_fab", "ecad_export_step"}) {
        bool present = false;
        for (int i = 0; i < ribbonCount; ++i)
            if (ui::ribbonRowActive(i) && std::string_view(ribbon[i].cmd_id) == command)
                present = true;
        check(present, command);
    }
    CommandContext noEcad;
    add("open is available without a document", "ecad_open", noEcad, true, true);
    add("save needs an electronics document", "ecad_save", noEcad, true, false);
    add("save as needs an electronics document", "ecad_save_as", noEcad, true, false);
    add("reload needs an electronics document", "ecad_reload", noEcad, true, false);
    add("PCB check needs a document", "ecad_run_drc", noEcad, true, false);
    add("schematic ERC", "ecad_run_erc", sch, true, true);
    add("board check on schematic", "ecad_run_drc", sch, true, false);
    add("board update needs an open pair", "pcb_update_from_sch", sch, true, false);
    add("new project does not need an active board", "ecad_new_project", sch, true, true);
    add("schematic opens in KiCad", "ecad_kicad_open_sch", sch, true, true);
    sch.hasOpenBoard = true;
    add("update can use an open background board", "pcb_update_from_sch", sch, true, true);
    CommandContext pcb = sch;
    pcb.activeDocKind = CommandDocKind::PCB;
    pcb.activeDocId = "pcb-1";
    add("PCB DRC", "ecad_run_drc", pcb, true, true);
    add("ERC needs the active schematic", "ecad_run_erc", pcb, true, false);
    add("schematic editor needs a schematic", "ecad_kicad_open_sch", pcb, true, false);
    add("PCB editor", "ecad_kicad_open_pcb", pcb, true, true);
    add("fabrication bundle", "ecad_export_fab", pcb, true, true);
    add("conflict save as", "ecad_save_as", pcb, true, true);
    add("reload bound KiCad file", "ecad_reload", pcb, true, true);
    add("STEP export", "ecad_export_step", pcb, true, true);
    add("project manager", "ecad_kicad_open_project", pcb, true, true);
    pcb.ecadHasProject = false;
    add("project manager needs a project", "ecad_kicad_open_project", pcb, true, false);
    pcb.ecadHasProject = true;
    pcb.kicadStatusKnown = false;
    add("checks wait for KiCad status", "ecad_run_drc", pcb, true, false);
    pcb.kicadStatusKnown = true;
    pcb.ecadHasPath = false;
    add("fabrication needs a bound board", "ecad_export_fab", pcb, true, false);
    add("reload needs a bound file", "ecad_reload", pcb, true, false);
    pcb.ecadHasPath = true;
    pcb.kicadAvailable = false;
    add("checks need KiCad", "ecad_run_drc", pcb, true, false);
    add("in-house autorouter works without KiCad", "ecad_autoroute", pcb, true, true);
    pcb.kicadAvailable = true;
    pcb.kicadPcbAvailable = false;
    add("PCB editor needs its executable", "ecad_kicad_open_pcb", pcb, true, false);
    pcb.activeDocId.clear();
    add("board command requires registry id", "ecad_autoroute", pcb, true, false);

    CommandContext cam = part;
    add("new CAM job creates missing state", "cam_new_job", cam, true, true);
    add("setup creates missing state", "cam_add_setup", cam, true, true);
    add("generate needs a job", "cam_generate", cam, true, false);
    add("post needs a job", "cam_post", cam, true, false);
    add("operation needs setup", "cam_op_face", cam, true, false);
    cam.camHasJob = true;
    add("simulation can generate paths automatically", "cam_simulate", cam, true, true);
    add("post can generate paths automatically", "cam_post", cam, true, true);
    cam.camHasSetup = true;
    add("extraction needs a source", "cam_extract", cam, true, false);
    add("operation needs a source", "cam_op_face", cam, true, false);
    cam.camHasCachedExtract = true;
    add("face operation can reuse cached extraction", "cam_op_face", cam, true, true);
    add("other operations still need their source", "cam_op_pocket", cam, true, false);
    cam.camHasSource = true;
    add("source-backed operation", "cam_op_pocket", cam, true, true);
    cam.camBusy = true;
    add("busy CAM refuses generation", "cam_generate", cam, true, false);
    add("busy CAM refuses mutation", "cam_op_face", cam, true, false);
    add("busy CAM refuses job replacement", "cam_new_job", cam, true, false);
    add("busy CAM refuses setup mutation", "cam_add_setup", cam, true, false);

    CommandContext sheet = part;
    add("base flange still opens its picker", "sm_base_flange", sheet, true, true);
    add("flatten requires sheet metal", "sm_flat_pattern", sheet, true, false);
    sheet.sheetMetalHasBase = true;
    add("flatten requires a bend", "sm_flat_pattern", sheet, true, false);
    sheet.sheetMetalHasBends = true;
    add("bent sheet can flatten", "sm_flat_pattern", sheet, true, true);
    add("flat export needs a compiled flat", "sm_export_flat_dxf", sheet, true, false);
    sheet.sheetMetalHasValidFlat = true;
    add("compiled flat can be exported", "sm_export_flat_dxf", sheet, true, true);
    drawing.sheetMetalHasBase = drawing.sheetMetalHasBends = drawing.sheetMetalHasValidFlat = true;
    add("background sheet metal cannot flatten", "sm_flat_pattern", drawing, true, false);
    add("background flat cannot be exported", "sm_export_flat_dxf", drawing, true, false);
    sheet.sheetMetalHasValidFlat = false;
    sheet.sheetMetalFlatStatusUnknown = true;
    add("unknown status is not treated as a failed flat", "sm_export_flat_dxf", sheet, true, true);
    CommandContext sheetUnknown = part;
    sheetUnknown.sheetMetalBaseStatusUnknown = true;
    sheetUnknown.sheetMetalBendStatusUnknown = true;
    add("unknown sheet status is not treated as a failed bend",
        "sm_flat_pattern", sheetUnknown, true, true);

    add("reserved command stays unavailable", "sk_spline_surface", sketch2d, true, false);
    add("retired reserved command cannot be dispatched", "sim_load_torque", sim, true, false);
    add("unknown internal navigation is not blanket-disabled", "internal_navigation", part, true, true);
    add("unknown schematic navigation is not kind-gated", "sch_internal_navigation", part, true, true);
    add("unknown topology navigation is not study-gated", "sim_topology_navigation", part, true, true);
    for (const Case& test : cases) {
        const ui::CommandAvailability result = ui::commandAvailability(test.command, test.context);
        check(result.visible == test.visible && result.enabled == test.enabled, test.label);
        if (result.visible && !result.enabled)
            check(result.reason && *result.reason, "visible disabled command has a reason");
    }
    if (failures) return 1;
    std::cout << "command availability and capability: PASS\n";
    return 0;
}
