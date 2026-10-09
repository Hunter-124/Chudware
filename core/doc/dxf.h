// Minimal DXF R12 (AC1009) ASCII writer for 2D drawings and flat patterns.
//
// Writes HEADER ($ACADVER, code page, extents/limits), TABLES (LTYPE CONTINUOUS / DASHED /
// CENTER, LAYER, STYLE STANDARD) and ENTITIES with native R12 entities only: POLYLINE
// (optional constant width for thick ISO 128 lines, optional linetype override), ARC,
// CIRCLE, SOLID (filled arrowheads), and TEXT with 72/73 alignment. TEXT strings are
// mapped from UTF-8 to R12 control codes (%%d %%p %%c), cp1252 and ASCII words.
// CRLF line endings.
#pragma once

#include <array>
#include <string>
#include <vector>

namespace chudware::doc {

// A polyline is a list of [x,y] points (mm).
using Polyline = std::vector<std::array<double, 2>>;

struct DxfLayerDef {
    std::string name;
    int color = 7;                      // ACI
    std::string ltype = "CONTINUOUS";   // CONTINUOUS | DASHED | CENTER
};

struct DxfPolyline {
    std::string layer;
    Polyline pts;
    double width = 0.0;   // constant width (mm); 0 = hairline
    std::string ltype;    // empty = BYLAYER
};

// CCW arc in degrees; `circle` emits a CIRCLE.
struct DxfArc {
    std::string layer;
    std::array<double, 2> center{0.0, 0.0};
    double radius = 0.0;
    double start_deg = 0.0;
    double end_deg = 360.0;
    bool circle = false;
    std::string ltype;
};

struct DxfSolid {
    std::string layer;
    std::array<std::array<double, 2>, 3> pts{};
};

// Single-line TEXT. halign: 0 left, 1 center, 2 right; valign: 0 baseline, 2 middle,
// 3 top. `text` is UTF-8; the writer maps it to R12.
struct DxfText {
    std::string layer;
    std::array<double, 2> pos{0.0, 0.0};
    double height = 3.0;
    std::string text;
    int halign = 0;
    int valign = 0;
};

struct DxfDocument {
    std::vector<DxfLayerDef> layers;   // declared in this order (after layer "0")
    std::vector<DxfPolyline> polylines;
    std::vector<DxfArc> arcs;
    std::vector<DxfSolid> solids;
    std::vector<DxfText> texts;
    bool has_limits = false;
    std::array<double, 4> limits{0.0, 0.0, 0.0, 0.0};  // minx, miny, maxx, maxy (sheet)

    void add_layer(const std::string& name, int color = 7,
                   const std::string& ltype = "CONTINUOUS") {
        for (const auto& l : layers)
            if (l.name == name) return;
        layers.push_back({name, color, ltype});
    }
};

// Build the DXF R12 text. `entity_count` (out) receives the number of drawable entities.
std::string build_dxf(const DxfDocument& doc, int* entity_count);

// Write `doc` at `path` (parent dirs created). Throws std::runtime_error when there are
// no entities or on a write failure. Returns the resolved absolute path.
std::string write_dxf(const std::string& path, const DxfDocument& doc);

// UTF-8 → R12 TEXT string (%%d %%p %%c, × → X, ⌴/⌵/↧ → CBORE/CSK/DEPTH, cp1252).
std::string dxf_text_string(const std::string& utf8);

}  // namespace chudware::doc
