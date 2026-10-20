// Stateful 2D DrawingSession — C++ port of the stateful drawing-session half of
// engine/doc/drawing.py (DrawingSession + new_drawing/add_view/add_section/
// add_detail/set_view/remove_view/add_dim/remove_annotation/get_drawing_full/
// drawing_bom). Closes the "stateful DrawingSession deferral" called out in
// doc/drawing.h + doc/export2d.h.
//
// A drawing session holds the drawing-document JSON (sheets -> views ->
// annotations) PLUS the source part document tree it projects. The drawing has no
// geometry of its own — every fetch RE-PROJECTS its views through the EXISTING HLR
// path (doc/drawing.cpp project_view / project_section / project_detail) against the
// source's freshly compiled bodies, so the drawing stays associative. The registry
// mirrors doc/session.cpp / ecad/session.cpp: a process-global map keyed by an opaque
// doc id, a revision bumped on every mutation, require_session(doc_id) lookup.
//
// A drawing with a live source id re-pulls its current tree and equation environment
// for associative projection. doc_close captures the final committed source and
// freezes the resulting views with an explicit stale warning. An inline drawing
// instead projects its fixed tree. source_revision tracks the source snapshot;
// the compiled HLR geometry is recomputed from it on fetch.
#pragma once

#include <map>
#include <string>
#include <cstdint>
#include <utility>

#include <nlohmann/json.hpp>

namespace chudware::doc {

bool dirty_drawing_count(std::uint64_t& count);  // nonblocking, false = unknown
using DrawingSaveRevisions = std::map<std::string, std::pair<long, long>>;
void drawings_saved(const DrawingSaveRevisions& revisions);

// --- persistence (W12+ drawing embedding in the .step+ container) --------------
// drawings_for_source: serialize every OPEN drawing whose live source is `source_doc_id`
// to {"drawings/<drawing_id>.json": <drawing doc JSON string>} for .step+ embedding on
// save (port of engine/doc/stepplus.py::_collect_drawing_entries). Empty when none.
std::map<std::string, std::string> drawings_for_source(
    const std::string& source_doc_id, DrawingSaveRevisions& revisions);
// Called after a source session has been removed from the document registry.
// Freeze every associated drawing on its final source tree/env and flag it stale.
void drawings_source_closed(const std::string& source_doc_id,
                            const nlohmann::json& source_doc,
                            const nlohmann::json& source_env, long source_revision);

// register_restored_drawing: re-register a saved drawing (its serialized aicad.drawing/1
// doc) as a FRESH session whose live source is re-pointed to `source_doc_id` (the just-
// reopened part), so it re-projects against the reopened bodies. Returns
// {doc_id, name, source_doc_id}. Throws OpError on a non-drawing doc. Port of
// engine/doc/drawing.py::register_restored_drawing.
nlohmann::json register_restored_drawing(const nlohmann::json& drawing_doc,
                                         const std::string& source_doc_id);
// Standalone editable drawing container (.draw+ / aicad.draw+/1). Save embeds the
// source document by default plus a stale-safe cache of the last projected sheets.
// Open returns {drawing_id, source_doc_id, source_state:live|embedded_opened|missing,
// name, path}; a missing source remains viewable from cache/views.json.
nlohmann::json drawing_save_standalone(const std::string& drawing_id,
                                       const std::string& path);
nlohmann::json drawing_open_standalone(const std::string& path);


// drawing_new: create a drawing referencing a source part. Two source models:
//   - LIVE (host path): pass `source_doc_id` = an OPEN C++ DocSession id ("doc-cpp...").
//     The drawing re-pulls that session's CURRENT tree on every fetch (associative,
//     matching engine/doc/drawing.py which references the open source session). On
//     close the inline source is refreshed to the FINAL tree/env and its views are
//     frozen with a stale warning, never reverted to the creation-time snapshot.
//   - INLINE (stateless probe / parity): leave `source_doc_id` empty and pass the source
//     part tree in `source_doc` (+ `env`); the drawing recompiles that fixed tree.
// `name` defaults to "<source name> - Drawing". Returns {doc_id, drawing:<tree>}.
nlohmann::json drawing_new(const nlohmann::json& source_doc, const nlohmann::json& env,
                           const std::string& name, const std::string& source_doc_id = "");

// drawing_set_source: replace the stored source tree (+env) and bump the source
// revision so all views re-project against the new bodies (mirrors an edit to the
// open source in the oracle). Returns {doc_id, revision, source_revision}.
nlohmann::json drawing_set_source(const std::string& doc_id, const nlohmann::json& source_doc,
                                  const nlohmann::json& env);

// Drawing display units are independent of the source after creation. Recompose
// existing numeric annotation text and return {doc_id, system, revision, drawing}.
nlohmann::json drawing_set_units(const std::string& doc_id, const std::string& system);

// Sheet lifecycle. A newly-created drawing starts with s1 active; callers may add
// further sheets and direct sheet-less view/table operations to the active sheet.
nlohmann::json drawing_add_sheet(const std::string& doc_id, const std::string& size,
                                 const std::string& name = "");
nlohmann::json drawing_list_sheets(const std::string& doc_id);
nlohmann::json drawing_set_active_sheet(const std::string& doc_id,
                                        const std::string& sheet_id);


// drawing_add_view: append a base|projected|iso view to a sheet. Returns
// {view_id, revision, view:<projected geometry>}.
nlohmann::json drawing_add_view(const std::string& doc_id, const std::string& type,
                                const std::string& orientation, const std::string& sheet_id,
                                const nlohmann::json& scale, const nlohmann::json& position,
                                const nlohmann::json& show_hidden);

// drawing_add_section: a SECTION view cutting the model relative to a parent view.
// `plane` = {normal,point} (the cut_line parent-frame form is resolved here too). `cut_line`
// accepts a 2-point straight cut OR a multi-point (offset/jogged) polyline. `opts` carries
// custom-section controls: {mode:"full|half", depth:(mm clip), reverse:bool}.
nlohmann::json drawing_add_section(const std::string& doc_id, const std::string& parent_view_id,
                                   const nlohmann::json& plane, const nlohmann::json& cut_line,
                                   const nlohmann::json& label, const nlohmann::json& scale,
                                   const nlohmann::json& position, const std::string& sheet_id,
                                   const nlohmann::json& show_hidden,
                                   const nlohmann::json& opts = nlohmann::json::object());

// drawing_add_detail: a magnified DETAIL view of a circular region of a parent view.
nlohmann::json drawing_add_detail(const std::string& doc_id, const std::string& parent_view_id,
                                  const nlohmann::json& center, double radius,
                                  const nlohmann::json& scale, const nlohmann::json& label,
                                  const nlohmann::json& position, const std::string& sheet_id,
                                  const nlohmann::json& show_hidden);

// drawing_remove_view: delete a view by id. Returns {removed, revision}.
nlohmann::json drawing_remove_view(const std::string& doc_id, const std::string& view_id);

// drawing_set_view: move / rescale / re-orient a view. Returns the re-projected geom.
nlohmann::json drawing_set_view(const std::string& doc_id, const std::string& view_id,
                                const nlohmann::json& scale, const nlohmann::json& position,
                                const nlohmann::json& orientation, const nlohmann::json& show_hidden);

// drawing_add_dim: append a dimension / mark / note annotation to a view.
nlohmann::json drawing_add_dim(const std::string& doc_id, const std::string& view_id,
                               const std::string& type, const nlohmann::json& a,
                               const nlohmann::json& b, const nlohmann::json& c,
                               const nlohmann::json& d, const nlohmann::json& pos,
                               const nlohmann::json& text);
// Structured manufacturing dimensions rendered by annot_draw.
nlohmann::json drawing_add_chamfer_dimension(const std::string& doc_id,
                                             const std::string& view_id,
                                             const nlohmann::json& spec);
nlohmann::json drawing_add_ordinate_dimensions(const std::string& doc_id,
                                               const std::string& view_id,
                                               const nlohmann::json& spec);


// drawing_remove_annotation: delete an annotation by id from a view.
nlohmann::json drawing_remove_annotation(const std::string& doc_id, const std::string& view_id,
                                         const std::string& ann_id);

// drawing_set_annotation: patch detailing fields on an existing annotation without
// recreating it (the "double-click to edit" / agent set_dim path). `patch` is an object
// holding any of: tol_type ("none|symmetric|deviation|limit|basic"), tol_upper, tol_lower,
// prefix, suffix, precision (int), critical (bool), count (int), pos ([x,y]), value (model
// override), text (verbatim display override; "" clears it). The on-sheet display text is
// recomposed from value + fields for numeric dims. Returns {annotation, drawing}.
nlohmann::json drawing_set_annotation(const std::string& doc_id, const std::string& view_id,
                                      const std::string& ann_id, const nlohmann::json& patch);

// drawing_add_callout: SolidWorks-style smart callouts attached to a view.
//   kind "hole"   -> snaps the nearest projected circle to `a`, emits "n× ⌀d" (count is
//                    the number of same-diameter holes in the view). opts: pos, suffix.
//   kind "balloon"-> item-number bubble (opts: item, pos, text); `a` is the leader anchor.
// Returns {annotation, drawing}.
nlohmann::json drawing_add_callout(const std::string& doc_id, const std::string& view_id,
                                   const std::string& kind, const nlohmann::json& a,
                                   const nlohmann::json& opts);

// --- GD&T (ASME Y14.5) annotations ---------------------------------------------
// Each appends a structured GD&T annotation to a view (composed into vector primitives by
// annot_draw on every drawing_get) and returns {annotation, drawing}, mirroring
// drawing_add_callout. `spec` is a compact object so the copilot/MCP can emit it directly.
//
// drawing_add_gdt: a feature control frame.
//   spec: {symbol:"position|flatness|perpendicularity|profile_surface|...",
//          tol_value:(number|string), tol_diameter:bool, tol_modifier:"none|mmc|lmc|projected",
//          datums:[{ref:"A", modifier:"none|mmc|lmc"}], row2:{...composite second row},
//          a:[x,y] (feature anchor), pos:[x,y] (frame location, sheet mm)}
nlohmann::json drawing_add_gdt(const std::string& doc_id, const std::string& view_id,
                               const nlohmann::json& spec);
// drawing_add_datum: a datum feature symbol (triangle + boxed letter).
//   spec: {label:"A", a:[x,y], pos:[x,y]}
nlohmann::json drawing_add_datum(const std::string& doc_id, const std::string& view_id,
                                 const nlohmann::json& spec);
// drawing_add_datum_target: a datum target (divided circle).
//   spec: {label:"A1", target_type:"point|line|area", size:"⌀6", a:[x,y], pos:[x,y]}
nlohmann::json drawing_add_datum_target(const std::string& doc_id, const std::string& view_id,
                                        const nlohmann::json& spec);
// drawing_add_surface_finish: a surface-texture symbol.
//   spec: {value:"1.6"|ra:1.6, machining:"any|required|prohibited", all_around:bool, lay:"...",
//          a:[x,y], pos:[x,y]}
nlohmann::json drawing_add_surface_finish(const std::string& doc_id, const std::string& view_id,
                                          const nlohmann::json& spec);
// drawing_add_weld: an AWS weld symbol.
//   spec: {weld_type:"fillet|square|vee|bevel|plug", size:"6", other_side:bool, all_around:bool,
//          a:[x,y], pos:[x,y]}
nlohmann::json drawing_add_weld(const std::string& doc_id, const std::string& view_id,
                                const nlohmann::json& spec);

// drawing_model_items: auto-dimension a view — overall width/height from the bbox plus a
// diameter callout per distinct projected circle. Annotations are tagged "auto":true.
// Returns the full drawing plus {added, view_id}.
nlohmann::json drawing_model_items(const std::string& doc_id, const std::string& view_id);

// drawing_set_titleblock: merge title-block fields (title, drawn_by, date, scale, sheet,
// rev, material, finish, ...) onto a sheet. Returns {title_block, drawing}.
nlohmann::json drawing_set_titleblock(const std::string& doc_id, const std::string& sheet_id,
                                      const nlohmann::json& fields);

// drawing_set_sheet: change a sheet's paper size: ISO A4..A0 / ANSI A..E, landscape by default;
// a trailing "P" (or "portrait") selects portrait ("A4P", "A3 portrait"). Returns {size, drawing}.
nlohmann::json drawing_set_sheet(const std::string& doc_id, const std::string& sheet_id,
                                 const std::string& size);

// drawing_arrange: professional auto layout of one sheet. Picks ONE standard scale (ISO 5455)
// that fits the free drawing area (frame minus title block and tables) unless `scale` is a
// number, places the standard views on the first- or third-angle lattice ("first"|"third";
// empty keeps the title block's projection, default third) with shared alignment, derived views
// beside their parent row, and records SCALE + projection in the title block. Annotations and
// derived cuts move with their views. Returns {doc_id, scale, scale_text, projection, drawing}.
nlohmann::json drawing_arrange(const std::string& doc_id, const std::string& sheet_id,
                               const std::string& projection, const nlohmann::json& scale);
// drawing_add_table: append a first-class structured on-sheet table.
// kind = bom|revision|holes|notes|cutlist. `spec` may carry anchor:[x,y],
// columns:[{key,title,width}], rows:[...], title, and table-specific options.
// BOM and hole rows are recomputed from the associative source/callouts on fetch.
nlohmann::json drawing_add_table(const std::string& doc_id, const std::string& sheet_id,
                                 const std::string& kind, const nlohmann::json& spec);


// drawing_make: ONE-SHOT drawing authoring for the copilot. From a live source part
// (source_doc_id) build a complete drawing in a single call and return {doc_id, drawing}. The
// spec is intentionally tiny so even small local models can emit it:
//   {
//     "name":   "<drawing name>",                  // optional
//     "sheet":  "A3",                               // optional ISO/ANSI size
//     "views":  ["front","top","right","iso"],      // default: front/top/right/iso
//     "model_items":   true,                        // auto-dimension each ortho view (default true)
//     "hole_callouts": false,                       // add ⌀ callouts to each distinct hole
//     "title":  {"title":"...","drawn_by":"...","material":"...","scale":"1:1","rev":"A", ...}
//   }
// Every step reuses the same associative engine path as the interactive tools, so the result is
// a fully editable drawing. Unknown view names are skipped (never throws on a bad view).
nlohmann::json drawing_make(const std::string& source_doc_id, const nlohmann::json& spec);

// drawing_get: the full drawing doc + every view's projected geometry (re-projected
// against the source's current bodies). Mirrors get_drawing_full.
nlohmann::json drawing_get(const std::string& doc_id);

// drawing_bom: a bill of materials for the drawing's source document. For an ASSEMBLY
// source: one row per DISTINCT part (grouped by source path, else name), with a rolled-up
// quantity, 1-based item, name, and source file. For a PART (or closed) source: empty rows
// (clean, not an error). Returns {rows:[{item,name,quantity,source}], is_assembly,
// source_doc_id}. Port of engine/doc/drawing.py::drawing_bom.
nlohmann::json drawing_bom(const std::string& doc_id);

// Place one ordinary, editable balloon per distinct assembly BOM item. Without view_id,
// prefer the view covering most items and use other views for any remaining visible items.
// Component projection runs only on this operation; placements describe the
// visible component outline anchor/bounds in sheet mm. Part drawings and items
// with no visible component refuse by name. Returns
// {added, view_ids, placements:[{item,view_id,component,source,anchor,bbox}], drawing}.
// Per-view projections are cached in view-local coordinates by source revision,
// view id/orientation/scale; moving a view reuses the mapping.
// Repeated runs replace only prior auto balloons.
nlohmann::json drawing_auto_balloon(const std::string& doc_id,
                                    const std::string& view_id = "");

// drawing_close: drop the session. Returns true if it existed.
bool drawing_close(const std::string& doc_id);

// CRASH RECOVERY (P0 deadlock fix — mirrors force_release_doc_lock in session.h).
// Drop an orphaned drawing-session g_mtx lock left held by THIS thread after a
// Windows structured exception (access violation) unwound past a DrawingLock without
// running its C++ destructor under /EHsc. Called from the libchudware C dispatch
// boundary's __except filter alongside force_release_doc_lock so ANY hardware fault
// releases ALL orphaned registry locks. Idempotent: no-ops if the lock is not held
// by the current thread. Returns true if it actually released an orphaned lock.
bool force_release_drawing_lock();

}  // namespace chudware::doc
