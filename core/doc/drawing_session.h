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
// SOURCE MODEL (vs. the Python oracle): the oracle references an OPEN source
// DocSession by id and pulls its last good bodies on fetch. The C++ core's drawing
// session stores the source document TREE (+ its equation env) supplied at
// drawing_new and recompiles it on fetch — the same associative re-projection, with
// the source carried inline (matching the stateless drawing probe in export2d.cpp).
// `source_revision` is bumped whenever the caller updates the stored source via
// drawing_set_source, so the per-(rev,orient,scale,hidden) projection cache stays
// valid exactly as in the oracle.
#pragma once

#include <map>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad::doc {

// --- persistence (W12+ drawing embedding in the .step+ container) --------------
// drawings_for_source: serialize every OPEN drawing whose live source is `source_doc_id`
// to {"drawings/<drawing_id>.json": <drawing doc JSON string>} for .step+ embedding on
// save (port of engine/doc/stepplus.py::_collect_drawing_entries). Empty when none.
std::map<std::string, std::string> drawings_for_source(const std::string& source_doc_id);

// register_restored_drawing: re-register a saved drawing (its serialized aicad.drawing/1
// doc) as a FRESH session whose live source is re-pointed to `source_doc_id` (the just-
// reopened part), so it re-projects against the reopened bodies. Returns
// {doc_id, name, source_doc_id}. Throws OpError on a non-drawing doc. Port of
// engine/doc/drawing.py::register_restored_drawing.
nlohmann::json register_restored_drawing(const nlohmann::json& drawing_doc,
                                         const std::string& source_doc_id);

// drawing_new: create a drawing referencing a source part. Two source models:
//   - LIVE (host path): pass `source_doc_id` = an OPEN C++ DocSession id ("doc-cpp...").
//     The drawing re-pulls that session's CURRENT tree on every fetch (associative,
//     matching engine/doc/drawing.py which references the open source session). The
//     inline `source_doc`/`env` then act only as the initial name + a fallback snapshot
//     if the source is later closed.
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

// drawing_add_view: append a base|projected|iso view to a sheet. Returns
// {view_id, revision, view:<projected geometry>}.
nlohmann::json drawing_add_view(const std::string& doc_id, const std::string& type,
                                const std::string& orientation, const std::string& sheet_id,
                                const nlohmann::json& scale, const nlohmann::json& position,
                                const nlohmann::json& show_hidden);

// drawing_add_section: a SECTION view cutting the model relative to a parent view.
// `plane` = {normal,point} (the cut_line parent-frame form is resolved here too).
nlohmann::json drawing_add_section(const std::string& doc_id, const std::string& parent_view_id,
                                   const nlohmann::json& plane, const nlohmann::json& cut_line,
                                   const nlohmann::json& label, const nlohmann::json& scale,
                                   const nlohmann::json& position, const std::string& sheet_id,
                                   const nlohmann::json& show_hidden);

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

// drawing_model_items: auto-dimension a view — overall width/height from the bbox plus a
// diameter callout per distinct projected circle. Annotations are tagged "auto":true.
// Returns the full drawing plus {added, view_id}.
nlohmann::json drawing_model_items(const std::string& doc_id, const std::string& view_id);

// drawing_set_titleblock: merge title-block fields (title, drawn_by, date, scale, sheet,
// rev, material, finish, ...) onto a sheet. Returns {title_block, drawing}.
nlohmann::json drawing_set_titleblock(const std::string& doc_id, const std::string& sheet_id,
                                      const nlohmann::json& fields);

// drawing_set_sheet: change a sheet's paper size (one of the ISO/ANSI keys A4..A0 / A..E).
// Returns {size, drawing}.
nlohmann::json drawing_set_sheet(const std::string& doc_id, const std::string& sheet_id,
                                 const std::string& size);

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

// drawing_close: drop the session. Returns true if it existed.
bool drawing_close(const std::string& doc_id);

}  // namespace aicad::doc
