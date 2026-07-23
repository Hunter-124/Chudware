// core/cam/operations.h — toolpath generators. Each generate_*() turns one
// Operation (+ its resolved geometry, the tool, and the setup context) into a
// Toolpath of classified moves. OCCT-free: geometry arrives as Regions / Path2 /
// points / a profile / a TriMesh inside Operation::geometry.
#pragma once

#include <string>
#include <vector>

#include "cam/model.h"

namespace chudware {
namespace cam {

// Context passed to every generator: the owning setup's machine/stock/wcs and the
// tool. Lets ops query stock top, machine kind, kerf, etc.
struct OpContext {
    Machine machine;
    Stock stock;
    WCS wcs;
    Tool tool;
    std::string material_id = "aluminum";
};

// Result of a generate call: the path plus any geometry/feeds warnings. On a hard
// failure `ok` is false and `error` explains (never throws).
struct GenResult {
    bool ok = true;
    std::string error;
    Toolpath path;
    std::vector<std::string> warnings;
};

// The single entry point: dispatches on op.type to the right generator. Resolves
// feeds (if unset) from the cutting DB, then builds the path.
GenResult generate(const Operation&, const OpContext&);

// ── individual generators (also directly unit-tested) ────────────────────────
// 2.5D milling
GenResult gen_face(const Operation&, const OpContext&);
GenResult gen_contour2d(const Operation&, const OpContext&);
GenResult gen_pocket2d(const Operation&, const OpContext&);
GenResult gen_slot(const Operation&, const OpContext&);
GenResult gen_adaptive2d(const Operation&, const OpContext&);
GenResult gen_engrave(const Operation&, const OpContext&);   // also Trace
GenResult gen_chamfer2d(const Operation&, const OpContext&);
GenResult gen_bore2d(const Operation&, const OpContext&);

// hole-making
GenResult gen_drill(const Operation&, const OpContext&);     // all canned cycles
GenResult gen_threadmill(const Operation&, const OpContext&);

// 3D milling (mesh / heightfield based)
GenResult gen_adaptive_clear3d(const Operation&, const OpContext&);
GenResult gen_pocket3d(const Operation&, const OpContext&);  // waterline roughing
GenResult gen_parallel3d(const Operation&, const OpContext&);
GenResult gen_contour3d(const Operation&, const OpContext&); // Z-level / waterline finish
GenResult gen_scallop3d(const Operation&, const OpContext&);
GenResult gen_pencil3d(const Operation&, const OpContext&);
GenResult gen_radial3d(const Operation&, const OpContext&);
GenResult gen_spiral_finish3d(const Operation&, const OpContext&);

// turning
GenResult gen_turn_face(const Operation&, const OpContext&);
GenResult gen_turn_rough(const Operation&, const OpContext&);
GenResult gen_turn_finish(const Operation&, const OpContext&);
GenResult gen_turn_groove(const Operation&, const OpContext&);
GenResult gen_turn_thread(const Operation&, const OpContext&);
GenResult gen_turn_part(const Operation&, const OpContext&);
GenResult gen_turn_drill(const Operation&, const OpContext&);

// 2D cutting heads + metrology
GenResult gen_cut2d(const Operation&, const OpContext&);
GenResult gen_probe(const Operation&, const OpContext&);

}  // namespace cam
}  // namespace chudware
