// ACIS SAT (text) / SAB (binary) reader: AutoCAD 3DSOLID / BODY / REGION / SURFACE payloads
// and standalone .sat/.sab files -> OCCT B-rep.
//
// Both encodings are tokenised into one record graph (SAT words/enums and SAB tags normalise
// to the same token kinds), then the topology body -> lump -> shell -> face -> loop -> coedge
// -> edge -> vertex -> point is rebuilt with shared TopoDS edges/vertices and healed with
// ShapeFix. Analytic surfaces/curves map to exact OCCT geometry; procedural spline surfaces
// and intcurves use the B-spline approximation ACIS stores in the record. Anything that
// cannot be built (unsupported type, approximation not stored) is named in `warnings`;
// nothing is dropped silently.
#pragma once

#include <TopoDS_Shape.hxx>

#include <string>
#include <string_view>
#include <vector>

namespace chudware::doc {

struct AcisBody {
    TopoDS_Shape shape;   // TopoDS_Solid for closed lumps, TopoDS_Shell/Compound for sheets/wires
    bool solid = false;   // every lump closed
};

struct AcisReadResult {
    std::vector<AcisBody> bodies;       // in ACIS model units, body transform applied
    double mm_per_unit = 0.0;           // header value (25.4 = inch); <= 0 when absent
    std::vector<std::string> warnings;  // named, per unsupported item; never silent
};

// `data`: SAT text (plain) or SAB bytes ("ACIS BinaryFile" | "ASM BinaryFile4" prefix). Throws
// std::runtime_error with a named reason when nothing usable can be built.
AcisReadResult read_acis(std::string_view data);

// DXF 1/3 group payload lines (in order; each entry is one complete SAT line: the caller has
// already appended group-3 continuations to their group-1 line) -> plain SAT text, lines
// joined with '\n'. Undoes AutoCAD's c -> 159-c "encryption" (c > 32) and the DXF caret
// escapes ("^ " is a literal '^', i.e. plain 'A'; "^J" etc. are control characters).
std::string decrypt_dxf_sat(const std::vector<std::string>& group_lines);

}  // namespace chudware::doc
