// ECAD document session — C++ port of engine/ecad/session.py (EcadSession +
// new/apply_ops/undo/redo/tree/close, revision counter, bounded JSON-snapshot
// undo/redo, stale-revision guard) over the ecad ops vocabulary.
//
// The ops-response shape mirrors the Python session: {revision, id_map, kind,
// tree, compile [, warnings]}; ecad_new adds "doc_id"; ecad_tree returns
// {revision, kind, tree, compile}. The `compile` field is live: schematics get
// netlist + ERC; boards get zone-fill refresh (interactive resolution), DRC,
// and ratsnest (session.cpp::compile_field).
#pragma once

#include <string>
#include <cstdint>

#include <nlohmann/json.hpp>

#include "ecad/ops.h"  // OpError

namespace chudware::ecad {

bool dirty_document_count(std::uint64_t& count);  // nonblocking, false = unknown

// ecad_new {kind: "sch"|"pcb", name?} -> ops-response + "doc_id".
nlohmann::json ecad_new(const std::string& kind, const std::string& name);

// ecad_apply_ops. base_revision < 0 skips the optimistic-concurrency guard;
// a mismatch throws OpError("stale_revision").
nlohmann::json ecad_apply_ops(const std::string& doc_id, long base_revision,
                              const nlohmann::json& ops);

nlohmann::json ecad_undo(const std::string& doc_id);     // bumps revision +1
nlohmann::json ecad_redo(const std::string& doc_id);     // bumps revision +1
nlohmann::json ecad_tree(const std::string& doc_id);     // no rebuild, no rev bump
nlohmann::json ecad_rebuild(const std::string& doc_id);  // recompute, no rev bump
bool ecad_close(const std::string& doc_id);

// ecad_swap_doc {doc_id, doc, base_revision?, warnings?} -> ops-response.
// Wholesale document replacement (engine/ecad/session.py::swap_doc): validate ->
// undo snapshot -> swap -> revision+1 -> compile. Throws stale_revision on mismatch.
nlohmann::json ecad_swap_doc(const std::string& doc_id, const nlohmann::json& new_doc,
                             long base_revision, const nlohmann::json& warnings);

// ecad_describe {doc_id, budget?} -> a compact but honest design summary for the AI:
// counts per entity kind, sheet list, net table (bounded by `budget` characters,
// default 4000), ERC/DRC severity tallies and the top violations. Never claims a
// clean design when the compile could not run.
nlohmann::json ecad_describe(const std::string& doc_id, long budget);

// ecad_resolve_pick {doc_id, xy, kinds?, tol?, layer?} -> {"selector": {...}} naming the
// topmost entity under a canvas point (schematic: pin > symbol > junction > label >
// power > no_connect > bus_entry > wire > bus > text > graphic > sheet_symbol;
// board: pad > via > track > zone > footprint). `selector` is null when nothing hits.
nlohmann::json ecad_resolve_pick(const std::string& doc_id, const nlohmann::json& xy,
                                 const nlohmann::json& kinds, double tol,
                                 const std::string& layer);

// ecad_design {request, name?} -> ops-response + "doc_id" + "recipe_used" + "design".
// Picks the closest deterministic recipe (ecad/recipes.h), then COMPILES it into a real
// schematic document: symbols placed on a 2.54 mm grid from the native symbol library,
// pin stubs, net labels, and power ports for supply nets. The produced document is a
// normal session (undoable, netlist-compiled, ERC-checked) — not an abstract dict.
nlohmann::json ecad_design(const std::string& request, const std::string& name);

// ecad_save {doc_id, path?} -> {"path", "format":"ecad+", "source_path"?}. Writes the
// open session's document as a .ecad+ ZIP container. Uses the session's NATIVE path
// when `path` is empty; throws OpError("io_error") if neither is available.
// FOREIGN-SOURCE SAFETY: a session opened from a .kicad_sch/.kicad_pcb keeps that file
// in `source_path` and never uses it as the native save target — a pathless Save writes
// the sibling "<stem>.ecad+" instead, so imported KiCad bytes can never be overwritten
// with a ZIP container. Use ecad_export(kind="kicad") to write KiCad text back out.
nlohmann::json ecad_save(const std::string& doc_id, const std::string& path);

// ecad_open {path} -> ops-response + "doc_id" + "warnings". Opens a .ecad+ container
// (engine/ecad/rpc.py::_ecad_open + formats.open), registers a session around the
// parsed document, rebuilds (compile), and returns the full response. Dispatches by
// extension like the Python rpc: .kicad_sch / .kicad_pcb route to the KiCad S-expr
// importers (ecad/kicad.cpp, port of engine/ecad/formats.py import_kicad_*); any
// import failure raises the same OpError("io_error", "import_kicad_* failed for
// '<path>': <exc>") envelope the oracle raises.
nlohmann::json ecad_open(const std::string& path);

// ecad_update_from_sch {board_doc_id, sch_doc_id, prune?, place_origin?,
// base_revision?} -> board ops-response + "report" + "doc_id". Cross-document
// back-annotation (engine/ecad/rpc.py::_ecad_update_from_sch + engine/ecad/sync):
// build the schematic's netlist, reconcile the board, swap the new board into the
// board session (one revision bump, undo snapshot, recompile), attach the report.
nlohmann::json ecad_update_from_sch(const std::string& board_doc_id, const std::string& sch_doc_id,
                                    bool prune, const nlohmann::json& place_origin,
                                    long base_revision);

// Stateless variant for the parity harness: reconcile an inline board against an
// inline schematic netlist (no sessions). Returns sync's {mutated_board, report}.
nlohmann::json ecad_update_from_sch_inline(const nlohmann::json& board,
                                           const nlohmann::json& sch_netlist, bool prune,
                                           const nlohmann::json& place_origin);

// ecad_schema {kind?} -> the empty document for the given kind (cheap diagnostic).
nlohmann::json ecad_schema(const std::string& kind);

// ecad_dfm {doc_id, rules?} -> {violations, summary} over the session's board doc.
// Throws OpError("invalid_op") if the document is not a pcb.
nlohmann::json ecad_dfm(const std::string& doc_id, const nlohmann::json& rules);

// Stateless variant: run DFM over an inline board doc (parity harness path).
nlohmann::json ecad_dfm_board(const nlohmann::json& board, const nlohmann::json& rules);

// ecad_export {kind, board|doc_id, path?, opts?} -> exported files.
//   pcb kinds: "gerber" | "drill" | "cpl" | "bom"
//   sch kinds: "netlist" | "bom" | "spice" | "kicad" | "svg" | "pdf"
// Returns {"files": {<logical-key>: <file-text>}, "paths": [<written path>, ...]}.
// `files` keeps the in-memory text so a remote/MCP caller can write it itself; when
// `path` is given the files are ALSO written here: a directory path (or one that does
// not look like a file) receives one conventionally-named file per key, a file path
// receives the single output directly. The logical key is the gerber layer_id
// ("F.Cu", ...), "pth"/"npth" for drill, "bom"/"cpl"/"netlist"/"spice"/"kicad" for the
// single-file outputs, or "<sheet-id>" for the per-sheet svg pages. A stateless `board`
// (parity-harness path) takes precedence over `doc_id`. Throws OpError("invalid_op")
// when the kind does not match the document kind, or for an unknown kind. PCB file
// CONTENT stays byte-identical to engine/ecad/fab/*.
nlohmann::json ecad_export(const nlohmann::json& params);

// ecad_impedance {mode, ...geom} -> the impedance.cpp calculator result.
// mode: "microstrip" | "stripline" | "coplanar" | "diff_pair" | "track".
// For "track", params carry {doc_id, track_or_net}. The other modes carry
// {w, s?, h, t, er, kind?}. Stateless except for "track" (session lookup).
nlohmann::json ecad_impedance(const nlohmann::json& params);

// ecad_autoroute {doc_id, opts?, base_revision?} -> board ops-response + "report"
// + "doc_id". Runs the grid autorouter on the open board session, applies the
// routed tracks/vias as pcb_add_track / pcb_add_via ops (one batch, one revision
// bump, undo snapshot, recompile), and attaches the router report (+ a failed-net
// warning). Mirrors engine/ecad/rpc.py::_ecad_autoroute exactly.
nlohmann::json ecad_autoroute(const std::string& doc_id, const nlohmann::json& opts,
                              long base_revision);

// ecad_route_diff_pair {doc_id, net_p, net_n, opts?, base_revision?} -> board
// ops-response + "report" + "doc_id". Same flow as ecad_autoroute but for one
// coupled differential pair (autoroute.route_diff_pair). NOTE: the Python worker
// has no ecad_route_diff_pair RPC handler (the oracle exposes only the autoroute
// compute); the C++ session adds the doc-id form the host's routeEcadToCpp expects,
// applying the routed geometry exactly like ecad_autoroute.
nlohmann::json ecad_route_diff_pair(const std::string& doc_id, const std::string& net_p,
                                    const std::string& net_n, const nlohmann::json& opts,
                                    long base_revision);

// ecad_fill_zones {doc_id, resolution?, base_revision?} -> board ops-response +
// "fill_summary" + "doc_id". Runs the FULL-resolution copper-pour fill (zonefill),
// swaps the filled board into the session (one revision bump, undo snapshot,
// recompile), and attaches {zones, zones_filled, polygons, copper_area_mm2}.
// Mirrors engine/ecad/rpc.py::_ecad_fill_zones exactly.
nlohmann::json ecad_fill_zones(const std::string& doc_id, const nlohmann::json& resolution,
                               long base_revision);

// ecad_simulate {doc_id, study?, sim_params?, job_id?} -> the solver envelope
// {ok, converged, cancelled?, results, warnings, log, convergence, ms}. Builds the
// schematic netlist, maps it to a SPICE circuit (netlist_to_spice_circuit), and
// runs the requested study (spice_simulate). A non-empty job_id registers the run
// with sim_cancel and is unregistered on every exit; cancellation returns
// ok:false and cancelled:true while retaining meaningful partial solver results.
// Mirrors engine/ecad/rpc.py::_ecad_simulate. Components without an inline "sim"
// mapping resolve from the starter SYMBOL sim table
// (core/sim/netlist.cpp::symbol_sim_table), same as the Python oracle. Unsupported
// components are warnings (not hard errors) and a fully unsupported schematic
// may produce {ok:false, "no simulatable devices ..."} when nothing simulates.
nlohmann::json ecad_simulate(const std::string& doc_id, const nlohmann::json& study,
                             const nlohmann::json& sim_params,
                             const std::string& job_id = {});

// Return a deep COPY of an open session's document (for cross-target callers like
// libchudware's OCCT-linked ecad_to_3d doc-id form, which lives outside chudware_ecad).
// Throws OpError("unknown_doc") if no such session is open.
nlohmann::json ecad_session_doc(const std::string& doc_id);

// CRASH RECOVERY (P0 deadlock fix — mirrors force_release_doc_lock in doc/session.h).
// Drop an orphaned ECAD session-store g_mtx lock left held by THIS thread after a
// Windows structured exception (access violation) unwound past an EcadLock without
// running its C++ destructor under /EHsc. Called from the libchudware C dispatch boundary's
// __except filter alongside force_release_doc_lock and force_release_drawing_lock so
// ANY hardware fault releases ALL orphaned registry locks. Idempotent: no-ops if the
// lock is not held by the current thread. Returns true if it released an orphaned lock.
bool force_release_ecad_lock();

}  // namespace chudware::ecad
