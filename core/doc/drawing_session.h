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

#include <string>

#include <nlohmann/json.hpp>

namespace aicad::doc {

// drawing_new: create a drawing referencing a source part `doc` (the tree, + `env`).
// `name` defaults to "<source name> - Drawing". Returns {doc_id, drawing:<tree>}.
nlohmann::json drawing_new(const nlohmann::json& source_doc, const nlohmann::json& env,
                           const std::string& name);

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

// drawing_get: the full drawing doc + every view's projected geometry (re-projected
// against the source's current bodies). Mirrors get_drawing_full.
nlohmann::json drawing_get(const std::string& doc_id);

// drawing_close: drop the session. Returns true if it existed.
bool drawing_close(const std::string& doc_id);

}  // namespace aicad::doc
