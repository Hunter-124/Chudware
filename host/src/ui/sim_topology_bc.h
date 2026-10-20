#pragma once
// sim_topology_bc.h — Topology-study boundary conditions, regions and material:
// the human editors for everything in the canonical topology study OUTSIDE the
// scalar `topology{}` object (which sim_topology.h owns), plus the pure helpers
// the integration owner (SimManager) needs to route viewport picks, paint
// previews, gate runs and build the native sim_run_topology envelope.
//
// CANONICAL STUDY (persisted; shared with the model tools — one dialect)
//   { id, type:"topology", name,
//     material: "<library key>" | { key?, name?, E_mpa?, nu?, yield_mpa?,
//                                   density_g_cm3?, k_w_mk?, alpha_per_k?,
//                                   cp_j_kgk? },
//     mesh_order: 1, mesh_target_mm?, mesh_fineness?, mesh_min_mm?,
//     fixtures:   [ <sel> | { faces:[<sel>...], dofs?:[0,1,2], label? } ],
//     loads:      [ { faces:[<sel>...], type:"force", vec:[N,N,N] (or "vector")
//     }
//                 | { faces, type:"pressure", magnitude: MPa (+ inward) } ],
//     load_cases: [ { name, weight?, fixtures?:[same as fixtures], loads:[same]
//     } ], preserve / exclude: [ { faces:[<sel>], thickness_mm?, label? }
//                         | { box_min_mm:[3], box_max_mm:[3], label? } ],
//     build_plate: {faces:[<sel>...], label?}, print_process:"<preset id>",
//     thermal: { reference_temperature_c?, fixtures:[{faces, temperature_c}],
//                loads:[ {faces,type:"flux",magnitude_w_m2} |
//                {faces,type:"power",power_w}
//                      | {faces,type:"convection",h_w_m2k,ambient_c} ] },
//     flow: { viscosity_pa_s, density_kg_m3, inlets:[{faces, velocity_m_s:[3],
//     temperature_c?}],
//             outlets:[{faces, pressure_pa}], walls:[{faces}], fluid_k_w_mk?,
//             fluid_cp_j_kgk?, brinkman_max?, reference_length_mm? },
//     topology: {...sim_topology.h...}, runs:[RunRecord...], last_job?,
//     <unknown keys kept> }
//   <sel> is a document face selector (kind:"face", optionally carrying the
//   viewport identity under "_pick":{body,topo}) or an already-resolved faceset
//   carrying "points" (used as is).
//
// OWNERSHIP RULES
//   Editors mutate the study IN PLACE and touch only the keys they display;
//   every other key (agent-authored, runs, sections of a physics not currently
//   selected) is preserved. A field cleared to empty ERASES its key — nothing
//   is silently defaulted; validateBoundaries() turns a missing required value
//   into a blocking issue. Nothing here issues an RPC or touches the
//   filesystem.
#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "sim_topology.h"

struct AppState;

namespace ui {
namespace topology {

// ---- physics applicability (mirrors core/sim/topopt_model uses_*_boundary)
// --------------------
bool usesStructuralBoundary(
    const std::string &physics); // structural modal buckling thermomechanical
bool usesStructuralLoads(
    const std::string &physics); // structural buckling thermomechanical
bool usesThermalBoundary(
    const std::string &physics); // thermal thermomechanical cht
bool usesFlowBoundary(const std::string &physics); // flow cht
std::string
studyPhysics(const nlohmann::json &study); // study.topology.physics or ""

// ---- face wells
// ---------------------------------------------------------------------------------
// A well is one `faces` array, addressed by its JSON pointer
// ("/fixtures/0/faces").
enum class WellRole { Support, Load, Preserve, Exclude, Port, Wall };
struct WellRef {
  std::string pointer;
  WellRole role = WellRole::Support;
  bool active = true; // the selected physics consumes this section
};
// Every well of the study in display order. A bare-selector fixture entry
// (static-study syntax) has no `faces` array; it is reported with pointer ""
// and never armed.
std::vector<WellRef> listWells(const nlohmann::json &study);
// Viewport identities ((body<<32)|topo) of every pick in ACTIVE wells of
// `role`. Bare-selector fixtures count as Support.
void collectFaceKeys(const nlohmann::json &study, WellRole role,
                     std::vector<std::uint64_t> &keys);
// Preserve/exclude boxes as flat GL_LINES xyz (12 edges each), mm == world
// units.
void collectRegionBoxSegments(const nlohmann::json &study, bool preserve,
                              std::vector<float> &segs);
// True when `pointer` addresses an existing faces array of the study.
bool wellExists(const nlohmann::json &study, const std::string &pointer);
// Toggle one viewport face in the well: removes it when present (by _pick
// identity), else appends `selector` stamped with "_pick". Returns +1 added, -1
// removed, 0 when the pointer does not address a faces array. `countAfter`
// receives the well size.
int toggleFace(nlohmann::json &study, const std::string &pointer,
               const nlohmann::json &selector, std::uint32_t body,
               std::uint32_t topo, std::size_t *countAfter = nullptr);
// Append selectors (de-duplicated by _pick identity, or canonical selector
// when either side lacks viewport identity). Returns how many were added.
std::size_t appendFaces(nlohmann::json &study, const std::string &pointer,
                        const std::vector<nlohmann::json> &selectors);

// New entries created by ribbon commands / "+" buttons. They carry NO numeric
// value: the user types it (missing values are blocking issues). Returns the
// new entry's faces pointer, or "" for a box region / when the kind does not
// apply.
enum class EntryKind {
  Fixture,
  ForceLoad,
  PressureLoad,
  PreserveFaces,
  PreserveBox,
  ExcludeFaces,
  ExcludeBox,
  ThermalFixture,
  ThermalFlux,
  ThermalPower,
  ThermalConvection,
  FlowInlet,
  FlowOutlet,
  FlowWall,
};
std::string addEntry(nlohmann::json &study, EntryKind kind, int loadCase = -1);
// The entry kind a generic ribbon "fixture" command creates for the study's
// physics (thermal/cht -> ThermalFixture, flow -> FlowWall, otherwise Fixture).
EntryKind fixtureKindFor(const std::string &physics);

// ---- material
// ------------------------------------------------------------------------------------
// Library lookup supplied by the integration owner: fills the physical
// constants the library actually records for `key` (no invented values). False
// when the key is unknown.
using MaterialLookup =
    std::function<bool(const std::string &key, nlohmann::json &physical)>;
// The normalized run material: recognized physical keys only (E_mpa, nu,
// yield_mpa, density_g_cm3, k_w_mk, alpha_per_k, cp_j_kgk, name). A key string
// resolves through the library; an object keeps its own values and fills ONLY
// absent keys from its `key`'s library entry. Returns false (with `error`) when
// a key cannot be resolved.
bool runMaterial(const nlohmann::json &study, const MaterialLookup &lookup,
                 nlohmann::json &out, std::string &error);
// Material keys the physics requires (mirrors topopt require_material).
std::vector<std::string> requiredMaterialKeys(const std::string &physics);

// ---- validation
// --------------------------------------------------------------------------------
// Boundary/material/mesh issues for the selected physics. Keys are JSON
// pointers of the offending value ("" for whole-setup issues). The native
// preparation stays authoritative.
std::vector<Issue> validateBoundaries(const nlohmann::json &study,
                                      const MaterialLookup &lookup);
// Transactional canonical definition edit. Keeps record identity/history and
// omitted unknown or inactive-physics sections. Checks native field types,
// capabilities and boundary ranges before changing `study`; error names the
// rejected parameter. Geometry resolution/buildRunEnvelope remain run gates.
bool applyStudyDefinition(nlohmann::json &study,
                          const nlohmann::json &definition,
                          const Capabilities &caps,
                          const MaterialLookup &lookup, std::string &error);


// ---- run envelope
// --------------------------------------------------------------------------------
// Unique document selectors (with "_pick" removed) that must be resolved
// through doc_face_points for the ACTIVE sections. Pre-resolved facesets are
// not listed.
std::vector<nlohmann::json> selectorsToResolve(const nlohmann::json &study);
// Canonical text key of one selector (the "_pick"-free dump) used to look up
// resolutions.
std::string selectorKey(const nlohmann::json &selector);
// Build the native sim_run_topology `study`: only the sections the physics
// consumes, every `faces` list replaced by ONE merged faceset (points
// concatenated, tol_mm = max; face_normal + perp_tol_mm kept when every member
// shares one face_normal; with no normals at all perp_tol_mm is kept and
// `surface` only when identical on every member — services/sim/topology.py
// rule), fixtures, walls and face regions as {faces:<faceset>,...}, force "vec"
// sent as "vector", the normalized material, mesh options and the scalar
// topology object. `resolved` maps selectorKey -> the doc_face_points reply.
// Refuses (false + error) on a missing resolution or material.
bool buildRunEnvelope(const nlohmann::json &study,
                      const std::map<std::string, nlohmann::json> &resolved,
                      const std::string &stepPath,
                      const nlohmann::json &material, nlohmann::json &out,
                      std::string &error);
// Stable digest of every input a run consumes (setup only: no runs, names, job
// state).
std::string setupKey(const nlohmann::json &study);

// ---- run records
// -----------------------------------------------------------------------------
// Compact, finite copy of a native result for persistence: drops element/node
// value arrays (density, fields[].values, overlay payloads), thins history to
// <= 256 samples, and turns non-finite numbers into null. Field descriptors
// keep {name, unit, location, components}.
nlohmann::json compactResult(const nlohmann::json &result);

// ---- views
// -------------------------------------------------------------------------------------
// Numeric drafts and parse errors live in the kit field store (kit_form.h).
// This per-study view carries document-safe face gestures, the armed well,
// the load case new loads land in and the one-shot scroll focus.
struct BcView {
  // Integration owner stamps the active document before drawing. Empty IDs
  // never accept or publish geometry payloads.
  std::string docId;
  std::string armedPointer; // faces array receiving viewport picks ("" none)
  int loadCase = -1; // load case last edited in the editor (-1 = shared)
  // One-shot scroll focus (ribbon commands, "Fix" actions): a JSON pointer of
  // an entry, a value inside one ("/loads/0/faces", "/loads/0/vec") or a list
  // ("/fixtures"). The next editor draw that shows the deepest matching entry
  // card (or the list's section) scrolls it into view, then clears it; a
  // pointer the drawn section does not show is cleared unconsumed. Arming a
  // well from outside the editor (armedPointer changed between draws) focuses
  // it the same way.
  std::string focusPointer;
  std::string seenArmed; // editor bookkeeping: armedPointer at the last draw
  int focusFrames = 0;   // editor bookkeeping: frames the focus has been held
};

struct BcResult {
  bool committed = false; // the study changed: persist it
  bool armChanged =
      false; // armedPointer changed: clear stale viewport selection
};

enum class BcSection { Boundaries, Regions, Material };
// Draws the section editor for the study's physics. Set view.docId to the
// active document first: every active well accepts additive face copies and
// picked face chips are draggable. `lockedReason` (non-null) renders it
// read-only (a run owns the study). Numeric controls and region boxes remain
// canonical inputs; unknown fields and other physics sections are retained.
BcResult drawBoundaryEditor(
    AppState &st, nlohmann::json &study, BcSection section, BcView &view,
    const MaterialLookup &lookup,
    const std::vector<std::pair<std::string, std::string>> &libraryMaterials,
    const char *lockedReason = nullptr);
// Draw the bed-face well inside the Additive manufacturing card.
BcResult drawBuildPlateEditor(AppState &st, nlohmann::json &study, BcView &view,
                              const char *lockedReason = nullptr);

} // namespace topology
} // namespace ui
