// ECAD -> 3D bridge: turn an aicad.pcb/1 BoardDocument into an OCCT solid model
// (substrate + analytic component proxies) — C++ port of engine/ecad/threed.py.
//
// WHY this lives in the OCCT-linked target (chudware_geom_occt), NOT chudware_ecad:
//   threed.py builds OCCT SOLIDS (extrude the Edge.Cuts outline by stackup
//   thickness, subtract drill cylinders, place per-footprint box proxies). The
//   pure-JSON ECAD library (chudware_ecad) is OCCT-free, so the bridge must sit in a
//   module that #includes both the ECAD board accessors (ecad/schema.h) and the
//   OCCT geom helpers (geom/features.h, booleans.h, transform.h, profile.h).
//
// SCOPE / PARITY:
//   The Python oracle's board_to_step() returns a {step_path, summary} where the
//   summary carries the load-bearing geometry numbers: outline_area, thickness,
//   board_volume (the SUBSTRATE solid volume AFTER drill subtraction), n_holes,
//   n_components, n_proxies, n_models, n_skipped, warnings. We reproduce the SAME
//   OCCT solids so BRep volume matches EXACTLY (same OCCT 7.9.3 kernel on both
//   sides). The gate is exact substrate volume + the deterministic analytic
//   total-assembly volume (substrate + proxies) + the summary counts.
//
//   DEFERRED (matched only in the analytic-proxy path): imported model_3d STEP
//   component bodies. The oracle loads them via engine.doc.formats.import_path and
//   their volume depends on the external STEP file's geometry, which is not
//   reproducible from the board doc alone. Our fixtures use model_3d=None (box
//   proxy), exactly like the oracle's test_threed.py, so the volume IS exact.
#pragma once

#include <nlohmann/json.hpp>

namespace chudware::ecad {

// Convert `board` (an aicad.pcb/1 doc dict) into a 3D model and return the
// geometry summary + volume stats. `opts` mirrors engine.ecad.threed
// BoardToStepOpts (components/copper/z_origin/copper_thickness); null = defaults.
//
// Returns a JSON object:
//   {
//     "summary": { outline_area, thickness, board_volume, n_holes, n_components,
//                  n_proxies, n_models, n_skipped, warnings:[...] },
//     "body_count": <substrate + placed components>,
//     "substrate_volume": <BRep volume of the drilled substrate solid>,
//     "total_volume": <substrate + analytic-proxy volumes summed>
//   }
//
// Throws chudware::ecad::OpError("invalid_op") for an empty / open / non-closing
// Edge.Cuts outline or a non-positive stackup thickness (the oracle raises
// BoardGeometryError — here it becomes the structured OpError the worker expects).
nlohmann::json board_to_3d(const nlohmann::json& board, const nlohmann::json& opts);

// Doc-id session form of ecad_to_3d: build the SAME 3D model as board_to_3d, write
// it to `out_path` as a STEP assembly (substrate + drilled holes + component box
// proxies), and return the oracle's board_to_step shape:
//
//   { "step_path": <abs out_path>, "summary": {...same as board_to_3d.summary...},
//     "body_count", "substrate_volume", "total_volume" }
//
// Mirrors engine/ecad/threed.py::board_to_step (which writes a STEP and returns
// {step_path, summary}); the extra volume fields are carried for the parity gate.
// Throws OpError("invalid_op") for a bad outline / thickness, OpError("io_error")
// if the STEP write fails.
nlohmann::json board_to_3d_step(const nlohmann::json& board, const nlohmann::json& opts,
                                const std::string& out_path);

}  // namespace chudware::ecad
