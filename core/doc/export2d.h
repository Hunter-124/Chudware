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

namespace aicad::doc {

// doc_drawing_views: compile `doc` with `env`, then HLR-project each view in `views`
// (each {orientation, scale?, position?, show_hidden?, view_id?}) against the
// surviving solid bodies. Returns {ok, body_count, views:[<project_view json>...]}.
// Throws OpError on a malformed request; a per-view projection failure is contained
// in that view's "error" (mirrors drawing.py).
nlohmann::json drawing_views(const nlohmann::json& doc, const nlohmann::json& env,
                             const nlohmann::json& views);

// export2d_drawing: compile `doc` with `env`, project `views`, pool every view's
// visible+hidden polylines into layers {VISIBLE, HIDDEN}, and write `path` as `fmt`
// ("dxf" | "pdf"). Returns {path, format, visible, hidden, entities}. Throws OpError
// ("export_failed") when there is no geometry, mirroring export2d.py::export_drawing.
nlohmann::json export2d_drawing(const nlohmann::json& doc, const nlohmann::json& env,
                                const nlohmann::json& views, const std::string& path,
                                const std::string& fmt);

// export_drawing: STATEFUL — pool every sheet/view of an OPEN drawing session (`doc_id`
// is a dwg-cpp id) into layers {VISIBLE, HIDDEN} and write `fmt` ("dxf"|"pdf"). The view
// geometry is already absolute sheet-mm (positioned by the session), so this just pools
// + writes. Returns {path, format, visible, hidden, entities}. Port of
// engine/doc/export2d.py::export_drawing. Throws OpError on no geometry / bad drawing.
nlohmann::json export_drawing(const std::string& doc_id, const std::string& path,
                              const std::string& fmt);

// export_flat_dxf: STATEFUL — the active sheet-metal PART's flat pattern (`doc_id` is a
// doc-cpp part id) written with the blank OUTLINE, BEND lines, and CUTS holes on SEPARATE
// layers (the key sheet-metal requirement). Derives the 2D blank from the compiled flat
// solid. Returns {path, format, bend_lines, outline_points, flat_area, entities}. Port of
// engine/doc/export2d.py::export_flat_dxf. Throws OpError when the part has no flat pattern.
nlohmann::json export_flat_dxf(const std::string& doc_id, const std::string& path,
                               const std::string& fmt);

}  // namespace aicad::doc
