// File import/export — C++ port of engine/doc/formats.py over OCCT's data-exchange
// toolkits (TKDESTEP / TKDEIGES / TKDESTL / TKRWMesh / TKXCAF …).
//
// B-rep lanes (P20): STEP / IGES / BREP read+write.
// Mesh lanes (P27, this increment): STL / OBJ / PLY / glTF / GLB read+write.
//   * EXPORT: surviving solids are tessellated (BRepMesh, Python defaults) and written
//     out as triangle meshes — STL via StlAPI_Writer, OBJ/PLY via deterministic hand
//     writers, glTF/GLB via OCCT's RWGltf_CafWriter (XCAF doc carrier).
//   * IMPORT: the triangle soup is read (RWStl/RWObj/RWGltf reader or the hand PLY
//     reader) into a Poly_Triangulation, then SEWN into a shell and (when closed)
//     promoted to a SOLID — the C++ analogue of the Python oracle's "as_solid"
//     conversion. The resulting TopoDS_Shape flows through the SAME import-feature
//     pipeline as a B-rep body (volume / bbox / AICM tessellation all work unchanged),
//     so the C++ core has no separate MeshBody type: a mesh becomes a real B-rep body
//     on import.
//
// Deferred (precise reasons — see formats.cpp): 3MF (lib3mf-only in the oracle; OCCT
// 7.9.3 ships no 3MF read/write) and 3dm (OpenNURBS/rhino3dm-only). The proprietary
// COM lanes (sldprt/sldasm/x_t/x_b) remain deferred as in P20.
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include <TopoDS_Shape.hxx>

namespace aicad::doc {

// Detect the format code from a path's extension: "step"/"iges"/"brep"/"stl"/"obj"/
// "ply"/"gltf"/"glb"/"3mf"/"3dm"/"sldprt"/… (mirrors formats.py::_EXT_TO_FORMAT).
// Throws OpError("invalid_op") on an unknown extension.
std::string detect_format(const std::string& path);

// A raw triangle-soup mesh (the C++ analogue of engine/doc/mesh_body.py::MeshBody used
// only internally by the mesh readers/writers — the public body model stays TopoDS).
// `verts` is N*3 doubles (x,y,z row-major); `tris` is M*3 0-based vertex indices.
struct MeshData {
    std::vector<double> verts;     // 3*N
    std::vector<uint32_t> tris;    // 3*M (0-based)
    std::size_t vertex_count() const { return verts.size() / 3; }
    std::size_t triangle_count() const { return tris.size() / 3; }
};

// Tessellate a TopoDS_Shape (solid or shell) into a triangle soup at the Python
// tessellation defaults (0.05mm linear / 0.35rad angular). Used by the mesh writers.
MeshData shape_to_mesh(const TopoDS_Shape& shape);

// Read a mesh file into a triangle soup (no sewing). Supported fmt: stl/obj/ply/gltf/
// glb/3mf. Throws OpError("import_failed") on an unsupported format or a read failure.
MeshData read_mesh(const std::string& path, const std::string& fmt);

// Read a mesh file into its RAW per-object (name, triangle-soup) list — NO sewing,
// NO concatenation, NO welding. For 3MF this is one entry per mesh object, the exact
// shape the Python oracle's _import_3mf returns. Used by the bit-parity gate to
// compare the raw lib3mf extraction (vertex coords + triangle indices + names) against
// the Python lib3mf path. Other mesh formats return a single ("<stem>", soup) entry.
std::vector<std::pair<std::string, MeshData>> read_mesh_raw(const std::string& path,
                                                            const std::string& fmt);

// Build a TopoDS_Shape from a triangle soup: sew the triangles into a shell, then
// promote to a SOLID when the shell is closed (the "as_solid" path). Returns the sewn
// shape (shell or solid). Empty mesh -> a null shape.
TopoDS_Shape mesh_to_shape(const MeshData& mesh);

// Write the surviving solid bodies to `path` in `fmt`. Supported: step, iges, brep,
// stl, obj, ply, gltf, glb (mesh formats tessellate the solids at the Python defaults).
// Bodies are merged into one compound for the B-rep writers (mirrors formats.py::
// _shapes_to_export_list). Throws OpError("export_failed") on an unsupported format or
// a writer failure. Returns the absolute path written.
std::string export_bodies(const std::vector<TopoDS_Shape>& bodies,
                          const std::string& path, const std::string& fmt);

// Read solid bodies from `path`. Supported: step (multi-solid), iges (sewn+healed),
// brep, and the mesh formats stl/obj/ply/gltf/glb (sewn into a shell/solid). Returns
// (name, shape) pairs in file order (Body1..N when the file carries no names). Throws
// OpError("import_failed") on an unsupported format or a read failure / empty result.
// 3mf / 3dm and the COM lanes (sldprt/sldasm/x_t/x_b) are NOT supported and raise
// import_failed with a precise reason.
std::vector<std::pair<std::string, TopoDS_Shape>> import_bodies(const std::string& path,
                                                                const std::string& fmt);

}  // namespace aicad::doc
