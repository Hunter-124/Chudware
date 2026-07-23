// core/cam/extract.h — the OCCT extraction adapter declaration. Turns a live
// B-rep part (a STEP/IGES/BREP/mesh file) into CAM geometry inputs (stock bounds,
// a triangle mesh, planar Z-section regions, and drill-hole points) that the
// OCCT-FREE operation generators consume.
//
// The DECLARATION is OCCT-free (json in / json out) so the libchudware dispatch can
// include it unconditionally; the IMPLEMENTATION (extract.cpp) is OCCT and lives
// in the chudware_doc lib (which links the OCCT data-exchange toolkits).
#pragma once

#include <nlohmann/json.hpp>

namespace chudware {
namespace cam {

// cam_extract {path, kind, ...} -> extracted geometry.
//   kind:
//     "stock"    -> {box:{min,max}, cylinder:{base,radius,height}, bbox}
//     "mesh"     -> {mesh:{verts,tris}}                  (for 3D ops)
//     "section"  -> {regions:[Region...]}  at params.z   (planar pocket/silhouette)
//     "sections" -> {levels:[{z, regions}...]} at params.z_levels (waterline)
//     "holes"    -> {points:[[x,y,z]...], holes:[{x,y,z,dia,depth}...]} (Z-axis cyl faces)
//     "profile"  -> {profile:[[z,x]...]}  the +X turning silhouette
//     "all"      -> stock + mesh + holes in one call
// `path` is a STEP/IGES/BREP/STL/OBJ/PLY/glTF file; tessellation deflection via
// params.deflection (mm, default 0.1). Never throws across the boundary (returns
// {"error":...} on failure).
nlohmann::json cam_extract(const nlohmann::json& params);

}  // namespace cam
}  // namespace chudware
