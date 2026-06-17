// File import/export — C++ port of engine/doc/formats.py over OCCT's data-exchange
// toolkits (TKDESTEP / TKDEIGES / TKDESTL …). NEUTRAL B-rep interchange only in this
// increment: STEP / IGES / BREP read+write + STL/OBJ/PLY/glTF WRITE (solids
// tessellated). Mesh IMPORT (→ a MeshBody) and the proprietary lanes (3MF/lib3mf,
// 3dm/OpenNURBS, SolidWorks/Parasolid COM) are deferred — they need representations /
// dependencies the C++ core does not have yet, and raise a clear error here.
#pragma once

#include <string>
#include <utility>
#include <vector>

#include <TopoDS_Shape.hxx>

namespace aicad::doc {

// Detect the format code from a path's extension: "step"/"iges"/"brep"/"stl"/"obj"/
// "ply"/"gltf"/"glb"/"3mf"/"3dm"/"sldprt"/… (mirrors formats.py::_EXT_TO_FORMAT).
// Throws OpError("invalid_op") on an unknown extension.
std::string detect_format(const std::string& path);

// Write the surviving solid bodies to `path` in `fmt`. Supported: step, iges, brep,
// stl, obj, ply, gltf, glb (the mesh formats tessellate the solids at 0.05mm/0.35rad,
// matching the Python tessellation defaults). Bodies are merged into one compound for
// the B-rep writers (mirrors formats.py::_shapes_to_export_list). Throws
// OpError("export_failed") on an unsupported format or a writer failure. Returns the
// absolute path written.
std::string export_bodies(const std::vector<TopoDS_Shape>& bodies,
                          const std::string& path, const std::string& fmt);

// Read solid bodies from `path`. Supported: step (multi-solid), iges (sewn+healed),
// brep. Returns (name, shape) pairs in file order (Body1..N when the file carries no
// names). Throws OpError("import_failed") on an unsupported format or a read failure /
// empty result. Mesh formats (stl/obj/ply/gltf/glb/3mf/3dm) and the COM lanes
// (sldprt/sldasm/x_t/x_b) are NOT yet supported and raise import_failed.
std::vector<std::pair<std::string, TopoDS_Shape>> import_bodies(const std::string& path,
                                                                const std::string& fmt);

}  // namespace aicad::doc
