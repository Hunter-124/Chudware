// Native AutoCAD R2000 (AC1015) DWG encoder for a DxfDocument.
//
// Writes the whole R13-R15 file layout from the Open Design Specification for .dwg files:
// file header with six section locators, AuxHeader, preview, header variables, classes,
// the object stream (bit-coded BS/BL/BD/DD/MC/MS/H/RD/TV/CMC/BE/BT, one CRC per object),
// the object map, ObjFreeSpace, the second header and the template section. The drawing
// carries the full set of objects AutoCAD expects in an R2000 file: every table control
// (BLOCK, LAYER, STYLE, LTYPE, VIEW, UCS, VPORT, APPID, DIMSTYLE, VX), *Model_Space and
// *Paper_Space block records with their BLOCK/ENDBLK, layer "0" plus the document layers,
// style Standard, linetypes ByBlock/ByLayer/Continuous/DASHED/CENTER, VPORT *Active, APPID
// ACAD, DIMSTYLE Standard and the named object dictionary (ACAD_GROUP, ACAD_LAYOUT with
// Model/Layout1, ACAD_MLINESTYLE with Standard, ACAD_PLOTSETTINGS, ACAD_PLOTSTYLENAME with
// Normal).
//
// Entity mapping: polylines -> LWPOLYLINE (closed when the last point repeats the first,
// constant width, per-entity linetype; past 20000 vertices — the most LibreDWG's reader,
// and so Chudware's own DWG import, accepts — consecutive LWPOLYLINEs sharing their joint
// vertex), arcs -> ARC/CIRCLE in their OCS (extrusion kept), lines3 -> LINE,
// faces3 -> 3DFACE (edge visibility kept), solids -> SOLID, texts -> TEXT with its
// horizontal/vertical alignment and alignment point, acis -> 3DSOLID (solid) / BODY
// (sheet) holding the SAT text as R2000 version-1 modeler geometry.
#pragma once

#include <cstddef>
#include <string>

#include "doc/dxf.h"

namespace chudware::doc {

struct DwgImage {
    std::string bytes;         // the complete .dwg file
    std::size_t entities = 0;  // model-space entities in it
};

// Encode `doc` as an R2000 DWG. Degenerate input that draws nothing (a polyline with
// fewer than two points, a zero radius, an empty or zero-height text) is not an entity,
// matching the DXF writer. Throws std::runtime_error naming the offending item for input
// R2000 cannot carry: an unknown linetype name, a layer colour outside ACI 1-255, a
// non-finite coordinate, a zero or non-finite arc extrusion, a TEXT alignment other than
// halign 0/1/2 + valign 0/2/3 or longer than 65535 bytes, an ACIS body whose SAT is empty,
// newer than ACIS 4.00 (version > 400), unterminated or not 7-bit.
DwgImage build_dwg(const DxfDocument& doc);

}  // namespace chudware::doc
