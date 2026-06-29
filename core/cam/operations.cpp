// core/cam/operations.cpp — the generate() dispatch over operation types. Each
// op type routes to its generator (op_*.cpp). The dispatch grows per CAM wave;
// types not yet wired in this build return a clear, non-throwing error envelope
// (never call an undefined generator).
#include "cam/operations.h"

#include "cam/op_common.h"

namespace aicad {
namespace cam {

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
        // ── 3D milling ──
        case OpType::AdaptiveClear3D: return gen_adaptive_clear3d(op, ctx);
        case OpType::Pocket3D:        return gen_pocket3d(op, ctx);
        case OpType::Parallel3D:      return gen_parallel3d(op, ctx);
        case OpType::Contour3D:       return gen_contour3d(op, ctx);
        case OpType::Scallop3D:       return gen_scallop3d(op, ctx);
        case OpType::Pencil3D:        return gen_pencil3d(op, ctx);
        case OpType::Radial3D:        return gen_radial3d(op, ctx);
        case OpType::SpiralFinish3D:  return gen_spiral_finish3d(op, ctx);
        // ── turning ──
        case OpType::TurnFace:   return gen_turn_face(op, ctx);
        case OpType::TurnRough:  return gen_turn_rough(op, ctx);
        case OpType::TurnFinish: return gen_turn_finish(op, ctx);
        case OpType::TurnGroove: return gen_turn_groove(op, ctx);
        case OpType::TurnThread: return gen_turn_thread(op, ctx);
        case OpType::TurnPart:   return gen_turn_part(op, ctx);
        case OpType::TurnDrill:  return gen_turn_drill(op, ctx);
        // ── 2D cutting + metrology ──
        case OpType::Cut2D:  return gen_cut2d(op, ctx);
        case OpType::Probe:  return gen_probe(op, ctx);
    }
    GenResult r;
    r.ok = false;
    r.error = std::string("unknown operation type: ") + to_string(op.type);
    return r;
}

}  // namespace cam
}  // namespace aicad
