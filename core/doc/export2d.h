// 2D drawing + export RPCs — C++ port of the stateless core of engine/doc/drawing.py
// (project_view) + engine/doc/export2d.py (export_drawing) over compile_document.
//
// SCOPE: the host's drawing_get geometry + export_drawing on the C++ core, in a
// STATELESS diagnostic form: the caller passes a full part document tree + env + a
// list of view definitions; we compile the part, HLR-project each view, and return
// the per-view geometry (and, for export, write the pooled VISIBLE/HIDDEN polylines
// to a DXF/PDF file). The stateful DrawingSession registry + the associative
// drawing_* lifecycle (new/add_view/set_view/add_dim/section/detail RPCs against an
// OPEN source session) is DEFERRED — this is the projection + export beachhead that
// the differential harness gates against the Python oracle.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

#include "doc/sheet_ink.h"

namespace chudware::doc {

// doc_drawing_views: compile `doc` with `env`, then HLR-project each view in `views`
// (each {orientation, scale?, position?, show_hidden?, view_id?}) against the
// surviving solid bodies. Returns {ok, body_count, views:[<project_view json>...]}.
// Throws OpError on a malformed request; a per-view projection failure is contained
// in that view's "error" (mirrors drawing.py).
nlohmann::json drawing_views(const nlohmann::json& doc, const nlohmann::json& env,
                             const nlohmann::json& views);

// export2d_drawing: compile `doc` with `env`, project `views`, pool every view's
// visible (thick) + hidden (dashed) polylines, and write `path` as `fmt` ("dxf" | "dwg" | "pdf" |
// "svg") on a page sized to the geometry. Returns {path, format, visible, hidden,
// entities}. Throws OpError ("export_failed") when there is no geometry.
nlohmann::json export2d_drawing(const nlohmann::json& doc, const nlohmann::json& env,
                                const nlohmann::json& views, const std::string& path,
                                const std::string& fmt);

// export_drawing: STATEFUL — pool every sheet of an OPEN drawing session (`doc_id` is a
// dwg-cpp id) via pool_drawing_sheet (frame, views, view decorations, annotations,
// tables) and write `fmt`: "pdf" one page per sheet, "svg" one file per sheet
// (<stem>_<sheet>.svg when several), "dxf"/"dwg" sheets laid left-to-right on ISO layers
// (VISIBLE, HIDDEN, CENTER, DIMENSIONS, HATCH, BORDER, TABLES). Returns {path, paths,
// format, visible, hidden, annotations, entities, page_count, sheets, sheet_mm, ...}.
// Throws OpError on no geometry / bad drawing.
nlohmann::json export_drawing(const std::string& doc_id, const std::string& path,
                              const std::string& fmt);

// export_flat_dxf: STATEFUL — the active sheet-metal PART's flat pattern (`doc_id` is a
// doc-cpp part id) written with the blank OUTLINE, BEND lines, and CUTS holes on SEPARATE
// layers (the key sheet-metal requirement). Derives the 2D blank from the compiled flat
// solid. Returns {path, format, bend_lines, outline_points, flat_area, entities}. Port of
// engine/doc/export2d.py::export_flat_dxf; `fmt` "dxf" | "dwg" | "pdf". Throws OpError when
// the part has no flat pattern.
nlohmann::json export_flat_dxf(const std::string& doc_id, const std::string& path,
                               const std::string& fmt);

// pool_drawing_sheet: bucket one drawing_get sheet into per-layer ISO 128 line classes
// (sheet["frame"] → frame; view visible → thick, hidden → dashed, hatch → thin;
// view["draw"] + annotation draw blocks → annotations, or tables for table overlays).
// The single pooling path behind every drawing export and drawing_render_png.
InkSheet pool_drawing_sheet(const nlohmann::json& sheet);

}  // namespace chudware::doc
