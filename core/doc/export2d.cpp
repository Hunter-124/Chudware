// 2D drawing + export RPCs — see export2d.h.

#include "doc/export2d.h"

#include <array>
#include <vector>

#include "doc/document.h"
#include "doc/drawing.h"
#include "doc/dxf.h"
#include "doc/pdfvec.h"
#include "doc/session.h"  // OpError

using nlohmann::json;

namespace aicad::doc {

namespace {

std::array<double, 2> get_pos(const json& v) {
    if (v.contains("position") && v["position"].is_array() && v["position"].size() >= 2)
        return {v["position"][0].get<double>(), v["position"][1].get<double>()};
    return {0.0, 0.0};
}

// Project every view def against `shapes`, returning the list of project_view JSON.
json project_all(const std::vector<TopoDS_Shape>& shapes, const json& views) {
    json out = json::array();
    int idx = 0;
    for (const auto& v : views) {
        std::string orient = v.value("orientation", std::string("front"));
        double scale = v.value("scale", 1.0);
        bool show_hidden = v.value("show_hidden", true);
        std::string vid = v.value("view_id", std::string("v") + std::to_string(++idx));
        out.push_back(project_view(shapes, orient, scale, get_pos(v), show_hidden, vid));
    }
    return out;
}

}  // namespace

json drawing_views(const json& doc, const json& env, const json& views) {
    if (!views.is_array())
        throw OpError("invalid_op", "drawing_views: 'views' must be an array");
    CompileResult res = compile_document(doc, env.is_null() ? json::object() : env);
    json out = {{"ok", res.ok}, {"body_count", res.body_count}};
    out["views"] = project_all(res.shapes, views);
    return out;
}

json export2d_drawing(const json& doc, const json& env, const json& views,
                      const std::string& path, const std::string& fmt) {
    if (!views.is_array())
        throw OpError("invalid_op", "export2d_drawing: 'views' must be an array");
    if (fmt != "dxf" && fmt != "pdf")
        throw OpError("invalid_op", "format must be 'dxf' or 'pdf'", {{"format", fmt}});
    CompileResult res = compile_document(doc, env.is_null() ? json::object() : env);
    json projected = project_all(res.shapes, views);

    // Pool visible + hidden polylines (already absolute sheet mm). Mirrors
    // export2d.py::_drawing_layers.
    std::vector<Polyline> visible, hidden;
    for (const auto& view : projected) {
        for (const auto& jpl : view.value("visible", json::array())) {
            if (!jpl.is_array() || jpl.size() < 2) continue;
            Polyline pl;
            for (const auto& jp : jpl) pl.push_back({jp[0].get<double>(), jp[1].get<double>()});
            visible.push_back(std::move(pl));
        }
        for (const auto& jpl : view.value("hidden", json::array())) {
            if (!jpl.is_array() || jpl.size() < 2) continue;
            Polyline pl;
            for (const auto& jp : jpl) pl.push_back({jp[0].get<double>(), jp[1].get<double>()});
            hidden.push_back(std::move(pl));
        }
    }
    int n = static_cast<int>(visible.size() + hidden.size());
    if (n == 0)
        throw OpError("export_failed",
                      "export2d_drawing: the drawing has no view geometry to export",
                      {{"path", path}});

    std::string out_path;
    try {
        if (fmt == "dxf") {
            DxfLayers layers;
            // Insertion order VISIBLE then HIDDEN (matches the Python dict order).
            layers.add_layer("VISIBLE");
            layers.add_layer("HIDDEN");
            for (const auto& pl : visible) layers.add_polyline("VISIBLE", pl);
            for (const auto& pl : hidden) layers.add_polyline("HIDDEN", pl);
            out_path = write_dxf(path, layers);
        } else {
            std::vector<PdfLine> lines;
            for (const auto& pl : visible) lines.push_back({pl, false});
            for (const auto& pl : hidden) lines.push_back({pl, true});
            out_path = write_pdf(path, lines);
        }
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& exc) {
        throw OpError("export_failed", std::string("export2d_drawing: ") + exc.what(),
                      {{"path", path}, {"format", fmt}});
    }

    return {{"path", out_path}, {"format", fmt},
            {"visible", static_cast<int>(visible.size())},
            {"hidden", static_cast<int>(hidden.size())}, {"entities", n}};
}

}  // namespace aicad::doc
