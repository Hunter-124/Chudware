// Minimal DXF R12 ASCII writer — C++ port of engine/doc/dxf.py (W14).
//
// Writes the small subset of DXF R12 (AC1009) the 2D drawing / sheet-metal flat
// layers need: a HEADER, a TABLES section declaring named LAYERS (color + linetype)
// + the LTYPE table (CONTINUOUS + DASHED), and an ENTITIES section of native R12
// POLYLINE/VERTEX/SEQEND (or LINE-per-segment) entities. Every polyline is already a
// discretized list of [x,y] points (the HLR output); a polyline is all we emit.
//
// The output is byte-identical to engine/doc/dxf.py::write_dxf for the same layers
// (same group-code order, same float formatting, CRLF line endings) so the
// differential harness can compare the DXF text exactly.
#pragma once

#include <array>
#include <map>
#include <string>
#include <vector>

namespace chudware::doc {

// A polyline is a list of [x,y] points (mm). Layers map a layer name (VISIBLE /
// HIDDEN / OUTLINE / BEND / CUTS / ...) to its polylines. We keep insertion order
// via a parallel name list so the TABLES + ENTITIES order matches the Python dict's
// insertion order (the oracle emits in dict order).
using Polyline = std::vector<std::array<double, 2>>;

// A single-line TEXT label placed at an insertion point (mm), at `height` mm. Used for
// drawing annotations (dimension values, hole callouts, title block) on export (Bug N-7).
struct DxfText {
    std::string layer;
    std::array<double, 2> pos;
    double height = 3.0;
    std::string text;
};

// Ordered layer set: names in order + the polylines per name. Mirrors the Python
// dict {layer_name: [polyline, ...]} (insertion-ordered). `texts` is an extra, ordered
// list of TEXT entities (empty in the geometry-only path, so plain drawings are byte-
// unchanged).
struct DxfLayers {
    std::vector<std::string> names;                  // layer order
    std::map<std::string, std::vector<Polyline>> polylines;  // name -> polylines
    std::vector<DxfText> texts;                      // annotation TEXT entities (ordered)

    void add_layer(const std::string& name) {
        if (polylines.find(name) == polylines.end()) {
            names.push_back(name);
            polylines[name] = {};
        }
    }
    void add_polyline(const std::string& name, const Polyline& pl) {
        add_layer(name);
        polylines[name].push_back(pl);
    }
    void add_text(const std::string& name, const std::array<double, 2>& pos, double height,
                  const std::string& text) {
        add_layer(name);
        texts.push_back({name, pos, height, text});
    }
};

// Build the DXF R12 text for `layers`. `polyline_as` is "polyline" (native R12
// POLYLINE, one entity per edge — the default) or "line" (one LINE per segment).
// `entity_count` (out) receives the number of polyline/line entities written.
// Returns the full DXF text (CRLF-joined, with a trailing CRLF). Empty layers are
// still declared in TABLES but contribute no entities.
std::string build_dxf(const DxfLayers& layers, const std::string& polyline_as,
                      int* entity_count);

// Write `layers` to a DXF R12 file at `path` (parent dirs created). Throws
// std::runtime_error when there is no drawable geometry (every layer empty) or on
// a write failure. Returns the resolved absolute path. Mirrors write_dxf's ValueError.
std::string write_dxf(const std::string& path, const DxfLayers& layers,
                      const std::string& polyline_as = "polyline");

}  // namespace chudware::doc
