// core/cam/operations.cpp — the generate() dispatch over operation types. Each
// op type routes to its generator (op_*.cpp). The dispatch grows per CAM wave;
// types not yet wired in this build return a clear, non-throwing error envelope
// (never call an undefined generator).
#include "cam/operations.h"

#include "cam/op_common.h"

namespace aicad {
namespace cam {

static GenResult not_wired(const Operation& op, const char* phase) {
    GenResult r;
    r.ok = false;
    r.error = std::string("operation '") + to_string(op.type) + "' lands in the " + phase + " wave";
    r.path.op_id = op.id;
    return r;
}

GenResult generate(const Operation& op, const OpContext& ctx) {
    if (op.suppressed) {
        GenResult r;
        r.ok = true;
        r.path.op_id = op.id;
        r.warnings.push_back("operation suppressed");
        return r;
    }
    switch (op.type) {
        // ── 2.5D milling + hole-making (this wave) ──
        case OpType::Face:       return gen_face(op, ctx);
        case OpType::Contour2D:  return gen_contour2d(op, ctx);
        case OpType::Pocket2D:   return gen_pocket2d(op, ctx);
        case OpType::Slot:       return gen_slot(op, ctx);
        case OpType::Chamfer2D:  return gen_chamfer2d(op, ctx);
        case OpType::Bore2D:     return gen_bore2d(op, ctx);
        case OpType::Adaptive2D: return gen_adaptive2d(op, ctx);
        case OpType::Engrave:
        case OpType::Trace:      return gen_engrave(op, ctx);
        case OpType::Drill:      return gen_drill(op, ctx);
        case OpType::ThreadMill: return gen_threadmill(op, ctx);
        // ── 3D milling (3d wave) ──
        case OpType::AdaptiveClear3D:
        case OpType::Pocket3D:
        case OpType::Parallel3D:
        case OpType::Contour3D:
        case OpType::Scallop3D:
        case OpType::Pencil3D:
        case OpType::Radial3D:
        case OpType::SpiralFinish3D: return not_wired(op, "3D");
        // ── turning (turn wave) ──
        case OpType::TurnFace:
        case OpType::TurnRough:
        case OpType::TurnFinish:
        case OpType::TurnGroove:
        case OpType::TurnThread:
        case OpType::TurnPart:
        case OpType::TurnDrill: return not_wired(op, "turning");
        // ── 2D cutting + metrology (cut wave) ──
        case OpType::Cut2D:
        case OpType::Probe:     return not_wired(op, "cutting/probe");
    }
    GenResult r;
    r.ok = false;
    r.error = std::string("unknown operation type: ") + to_string(op.type);
    return r;
}

}  // namespace cam
}  // namespace aicad
