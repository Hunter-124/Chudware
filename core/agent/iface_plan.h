// core/agent/iface_plan.h — interface-aware assembly planning helpers.
//
// Pure (mostly) utilities shared by run_assembly_design and run_mission so mating
// parts are never designed blind in parallel:
//   1. Parse plan.interfaces / mission interfaces
//   2. Derive build order (seed before dependent)
//   3. Fold interface deps into swarm wave graphs
//   4. Build advisory injection text (measured mate OR binding seed standard)
//   5. Compare measured interface geometry and route fixes to the DEPENDENT
#pragma once

#include <map>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad {
namespace agent {

using nlohmann::json;

// One mating relationship declared by the planner.
struct InterfaceSpec {
    std::string id;                          // optional stable id
    std::vector<std::string> between;        // exactly two part names (preferred)
    std::string kind;                        // bolted_joint | pin | face | ...
    std::string seed;                        // part that FIXES the standard (empty = first)
    json params = json::object();            // holes, fit, spacing, etc. (pass-through)
    std::string raw_summary;                 // compact one-line for prompts
};

// Parsed interface ledger for one plan / mission.
struct InterfaceLedger {
    std::vector<InterfaceSpec> interfaces;
    // part_name -> names it depends on (must build after). Seed is never listed as dependent.
    std::map<std::string, std::vector<std::string>> part_deps;
    // part_name -> true when this part is seed for at least one interface.
    std::map<std::string, bool> is_seed;
};

// Parse plan.interfaces (array of objects). Tolerates alternate keys:
// between|parts|mates, seed|seed_part|fixes, kind|type, holes/fit nested or flat.
// Also accepts top-level "fit" string on the plan as a soft cue (not structured).
InterfaceLedger parse_interface_ledger(const json& plan);

// Topological order of part indices (0..n-1) given part names + ledger.
// Seeds and parts with no interface deps keep relative planner order among peers.
// Unknown interface names are ignored. Cycles → best-effort original order tail.
std::vector<size_t> order_parts_by_interfaces(const std::vector<std::string>& part_names,
                                              const InterfaceLedger& ledger);

// Fold interface deps into a task dependency list.
// tasks: [{id, title?, request?, deps?:[id,...], part?: name matching interface part}]
// When a task's part (or title/request token) matches a dependent interface part, and
// another task matches the seed, ensure seed_id is in dependent.deps.
// Returns new tasks array (deps arrays updated; other fields preserved).
json fold_interface_deps_into_tasks(const json& tasks, const InterfaceLedger& ledger);

// Compute wave membership from id+deps (Kahn). Same contract as run_mission swarm waves.
// Returns vector of waves; each wave is task indices. Dependent never shares a wave with
// a seed it depends on (when deps were folded).
std::vector<std::vector<size_t>> waves_from_task_deps(const json& tasks);

// ---- Injection text -------------------------------------------------------

// Measured geometry snapshot extracted from a part doc tree (+ optional bbox/mass).
struct MeasuredInterface {
    std::string part_name;
    std::string doc_id;
    json bbox = json::object();       // {size:[dx,dy,dz], min, max} if known
    json holes = json::array();       // [{type, diameter, count?, pcd?, positions?, id?}]
    std::string tree_digest;          // compact feature lines
    std::string summary;              // human one-liner
};

// Pull hole/bolt_circle (+ bbox if provided) from a feature-tree JSON.
// tree may be {features:[...]} or a full doc_tree payload with nested "tree".
MeasuredInterface measure_interface_from_tree(const std::string& part_name,
                                              const json& tree_or_doc,
                                              const json& bbox = json::object());

// Build the advisory block injected into a part sub-run via options.advisory_text.
// For each interface the part participates in:
//   (a) mate already measured → "ALREADY EXISTS ... match them EXACTLY"
//   (b) part is seed and mate not built → "you are the SEED — these dimensions are BINDING"
//   (c) part is dependent and mate not built → still emit plan interface as the standard
//       the seed will set (and note mate is pending).
// built: map part_name -> MeasuredInterface for parts already finished.
std::string build_interface_advisory(const std::string& part_name,
                                     const InterfaceLedger& ledger,
                                     const std::map<std::string, MeasuredInterface>& built);

// ---- Verification / fix routing ------------------------------------------

struct InterfaceMismatch {
    std::string interface_id;
    std::string kind;
    std::string seed_part;
    std::string dependent_part;   // ALWAYS the non-seed — fix target
    std::string detail;           // human expected-vs-actual
    json expected = json::object();
    json actual = json::object();
};

// Compare measured geometry of both sides against the interface spec.
// Seed dimensions WIN: mismatches always list dependent_part as the fix target.
// hole dia tolerance default 0.15 mm; pattern count must match when both known.
std::vector<InterfaceMismatch> verify_interfaces(
    const InterfaceLedger& ledger,
    const std::map<std::string, MeasuredInterface>& built,
    double dia_tol_mm = 0.15);

// Advisory text for a targeted fixer/modeler re-run on the DEPENDENT part.
std::string build_mismatch_fix_advisory(const std::vector<InterfaceMismatch>& mismatches,
                                        const std::string& dependent_part);

// True when `name` is the seed for interface `iface` (explicit seed, else between[0]).
bool interface_seed_is(const InterfaceSpec& iface, const std::string& name);

// The other participant in a 2-part interface, or "" if not found / not binary.
std::string interface_mate_of(const InterfaceSpec& iface, const std::string& name);

// Normalize a part token for matching (lower, strip non-alnum to _, collapse _).
std::string iface_norm_name(const std::string& s);

}  // namespace agent
}  // namespace aicad
