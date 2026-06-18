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

}  // namespace aicad::doc
