// ACIS SAT writer: OCCT B-rep -> SAT text that AutoCAD reads as editable solids.
//
// Version 400 is what AutoCAD/ODA embed in R2000 DWG/DXF (no entity ids, no edge
// parameters); version 700 is the standalone .sat layout AutoCAD's ACISIN reads (every
// entity carries a `-1` id and a `$-1` pattern pointer, edges carry start/end parameters and
// convexity, spline data carries the `full` approximation level). See acis_write.cpp for the
// record-level contract.
#pragma once

#include <string>
#include <utility>
#include <vector>

class TopoDS_Shape;

namespace chudware::doc {

struct SatWriteOptions {
    int version = 700;           // 400 (embedding in R2000 DWG/DXF) or 700 (standalone .sat)
    double mm_per_unit = 1.0;    // header units; the shape is already in these units
};

// One SAT file holding one ACIS body per non-null input shape (header body count = number
// of bodies written, body records first). Each shape's solids become lumps of single-sided
// faces; its free shells and connected groups of free faces become sheet lumps of
// double-sided faces. Throws std::runtime_error naming the geometry or topology it cannot
// express (free wires/vertices, unconvertible surfaces, failed self-checks) and when every
// input shape is null.
std::string write_sat_bodies(const std::vector<TopoDS_Shape>& bodies, const SatWriteOptions& opt);

// write_sat_bodies({shape}, opt): all solids/shells of `shape` become lumps of one body.
std::string write_sat(const TopoDS_Shape& shape, const SatWriteOptions& opt);

// Plain SAT -> DXF group payload for 3DSOLID/BODY/REGION: (1, line) starts each SAT line,
// (3, rest) continues it when the encrypted line exceeds 255 characters (a `^ ` escape is
// never split). Encryption: printable c (33..126) -> 159 - c, space kept, 'A' -> "^ ".
// Throws std::runtime_error on bytes outside printable ASCII (they cannot be encrypted).
std::vector<std::pair<int, std::string>> encrypt_dxf_sat(const std::string& sat);

}  // namespace chudware::doc
