// DXF ASCII writer for 2D drawings, flat patterns and part export. CRLF line endings.
//
// Documents without ACIS bodies are written as R12 (AC1009) — the laser/waterjet path,
// byte-stable: HEADER ($ACADVER, code page, extents/limits), TABLES (LTYPE CONTINUOUS /
// DASHED / CENTER, LAYER, STYLE STANDARD) and ENTITIES with native R12 entities only:
// POLYLINE (optional constant width for thick ISO 128 lines, optional linetype override),
// ARC, CIRCLE (with their OCS extrusion 210 when not +Z), SOLID (filled arrowheads), TEXT
// with 72/73 alignment, and the 3D part-export entities LINE and 3DFACE.
//
// Documents with `acis` bodies are written as R2000 (AC1015), the oldest DXF that holds
// solids: HEADER ($HANDSEED, $INSUNITS, $MEASUREMENT, 3D extents incl. the bodies),
// CLASSES, TABLES with handles/owners/subclass markers (VPORT *Active, LTYPE ByBlock /
// ByLayer / Continuous / DASHED / CENTER, LAYER, STYLE Standard, VIEW, UCS, APPID ACAD,
// DIMSTYLE Standard, BLOCK_RECORD *Model_Space / *Paper_Space), BLOCKS, ENTITIES
// (LWPOLYLINE, ARC, CIRCLE, SOLID, TEXT, LINE, 3DFACE, and 3DSOLID / BODY whose SAT is
// encrypted into 1/3 groups) and OBJECTS (root dictionary, ACAD_GROUP, ACAD_LAYOUT with
// Model / Layout1, ACAD_MLINESTYLE, ACAD_PLOTSETTINGS, ACAD_PLOTSTYLENAME). Bodies must be
// SAT version <= 400; unknown linetypes, invalid layer names and $INSUNITS outside 0..20
// are refused by name.
//
// TEXT strings are mapped from UTF-8 to control codes (%%d %%p %%c), cp1252 and ASCII
// words in both versions. The same DxfDocument also feeds the DWG writer (dwg.h).
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
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

// CCW (about the extrusion) arc in degrees; `circle` emits a CIRCLE. `center`/`z` are
// coordinates in the arc's Object Coordinate System (DXF Arbitrary Axis Algorithm frame of
// `normal`), so for the default +Z extrusion they are plain WCS x, y, z. Producers express
// arcs drawn about -Z by swapping their end angles.
struct DxfArc {
    std::string layer;
    std::array<double, 2> center{0.0, 0.0};
    double radius = 0.0;
    double start_deg = 0.0;
    double end_deg = 360.0;
    bool circle = false;
    std::string ltype;
    double z = 0.0;
    std::array<double, 3> normal{0.0, 0.0, 1.0};  // extrusion (210/220/230); need not be unit
};

// True when `n` is the default extrusion (+Z, any positive length): writers then omit
// 210/220/230 and DWG writes the one-bit default.
inline bool dxf_normal_is_z(const std::array<double, 3>& n) {
    return n[0] == 0.0 && n[1] == 0.0 && n[2] > 0.0 && std::isfinite(n[2]);
}

// `n` scaled to unit length; false when it is not finite or shorter than 1e-12.
inline bool dxf_unit_normal(const std::array<double, 3>& n, std::array<double, 3>& out) {
    const double len = std::sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]);
    if (!std::isfinite(len) || len < 1e-12) return false;
    out = {n[0] / len, n[1] / len, n[2] / len};
    return true;
}

// OCS -> WCS for the unit extrusion `n` (DXF reference, Arbitrary Axis Algorithm): the OCS
// X axis is Wy x N when N is within 1/64 of the world Z axis, else Wz x N; Y = N x X.
inline std::array<double, 3> dxf_ocs_to_wcs(const std::array<double, 3>& n,
                                            const std::array<double, 3>& p) {
    std::array<double, 3> ax;
    if (std::fabs(n[0]) < 1.0 / 64.0 && std::fabs(n[1]) < 1.0 / 64.0)
        ax = {n[2], 0.0, -n[0]};   // (0,1,0) x N
    else
        ax = {-n[1], n[0], 0.0};   // (0,0,1) x N
    const double l = std::sqrt(ax[0] * ax[0] + ax[1] * ax[1] + ax[2] * ax[2]);
    ax = {ax[0] / l, ax[1] / l, ax[2] / l};
    const std::array<double, 3> ay = {n[1] * ax[2] - n[2] * ax[1], n[2] * ax[0] - n[0] * ax[2],
                                      n[0] * ax[1] - n[1] * ax[0]};
    return {p[0] * ax[0] + p[1] * ay[0] + p[2] * n[0], p[0] * ax[1] + p[1] * ay[1] + p[2] * n[1],
            p[0] * ax[2] + p[1] * ay[2] + p[2] * n[2]};
}

// WCS bounding box of the full circle of `a`: the centre mapped OCS -> WCS, each axis k
// spanning radius * sqrt(1 - n_k^2). For a +Z arc this is exactly centre +- radius in x/y at
// z. An invalid extrusion counts as +Z here (the writers refuse it by name).
inline void dxf_arc_box(const DxfArc& a, std::array<double, 3>& lo, std::array<double, 3>& hi) {
    std::array<double, 3> n{0.0, 0.0, 1.0};
    if (!dxf_normal_is_z(a.normal) && !dxf_unit_normal(a.normal, n)) n = {0.0, 0.0, 1.0};
    const std::array<double, 3> c = dxf_normal_is_z(n)
                                        ? std::array<double, 3>{a.center[0], a.center[1], a.z}
                                        : dxf_ocs_to_wcs(n, {a.center[0], a.center[1], a.z});
    for (int k = 0; k < 3; ++k) {
        const double half = a.radius * std::sqrt(std::max(0.0, 1.0 - n[k] * n[k]));
        lo[k] = c[k] - half;
        hi[k] = c[k] + half;
    }
}

// A 3D line segment (part export).
struct DxfLine3 {
    std::string layer;
    std::array<double, 3> a{0.0, 0.0, 0.0};
    std::array<double, 3> b{0.0, 0.0, 0.0};
};

// A triangular 3DFACE (part export). `hidden_edges` bit i hides the edge from corner i to
// corner i+1 (bit 2 = corner 3 -> 1); interior tessellation edges are hidden so a viewer
// shows the B-rep face boundaries.
struct DxfFace3 {
    std::string layer;
    std::array<std::array<double, 3>, 3> pts{};
    int hidden_edges = 0;
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

// An ACIS body carried as plain (unencrypted) SAT text — header, records and the
// End-of-ACIS-data terminator — in the version `acis_version` it was written in.
// Writers embed it as a 3DSOLID (closed solid) or BODY (sheet / open shell) entity;
// the DXF writer encrypts it into 1/3 groups, the DWG writer stores it inline. Its
// coordinates are already in document units (see DxfDocument::insunits).
struct DxfAcis {
    std::string layer;
    std::string sat;
    bool solid = true;
    int acis_version = 400;
};

struct DxfDocument {
    std::vector<DxfLayerDef> layers;   // declared in this order (after layer "0")
    std::vector<DxfPolyline> polylines;
    std::vector<DxfArc> arcs;
    std::vector<DxfSolid> solids;
    std::vector<DxfText> texts;
    std::vector<DxfLine3> lines3;
    std::vector<DxfFace3> faces3;
    std::vector<DxfAcis> acis;         // non-empty -> DXF is written as R2000 (AC1015)
    // $INSUNITS code of every coordinate in this document (4 = mm, 1 = inch, 0 = unitless).
    // Producers convert to that unit before filling the document; writers only record it.
    int insunits = 4;
    bool has_limits = false;
    std::array<double, 4> limits{0.0, 0.0, 0.0, 0.0};  // minx, miny, maxx, maxy (sheet)

    void add_layer(const std::string& name, int color = 7,
                   const std::string& ltype = "CONTINUOUS") {
        for (const auto& l : layers)
            if (l.name == name) return;
        layers.push_back({name, color, ltype});
    }
};

// Build the DXF text (R12, or R2000 when `doc.acis` is non-empty). `entity_count` (out)
// receives the number of drawable entities. Throws std::runtime_error (R2000 only) on an
// input it cannot represent, naming it.
std::string build_dxf(const DxfDocument& doc, int* entity_count);

// Write `doc` at `path` (parent dirs created). Throws std::runtime_error when there are
// no entities or on a write failure. Returns the resolved absolute path.
std::string write_dxf(const std::string& path, const DxfDocument& doc);

// UTF-8 → R12 TEXT string (%%d %%p %%c, × → X, ⌴/⌵/↧ → CBORE/CSK/DEPTH, cp1252).
std::string dxf_text_string(const std::string& utf8);

}  // namespace chudware::doc
