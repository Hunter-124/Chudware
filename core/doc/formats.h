// File import/export — C++ port of engine/doc/formats.py over OCCT's data-exchange
// toolkits (TKDESTEP / TKDEIGES / TKDESTL / TKRWMesh / TKXCAF …).
//
// B-rep lanes (P20): STEP / IGES / BREP read+write.
// Mesh lanes (P27, this increment): STL / OBJ / PLY / glTF / GLB read+write.
//   * EXPORT: surviving solids are tessellated (BRepMesh, Python defaults) and written
//     out as triangle meshes — STL via RWStl with native-pole exclusion and exact
//     serialized solid-closure checks, OBJ/PLY via deterministic hand writers,
//     glTF/GLB via OCCT's RWGltf_CafWriter (XCAF doc carrier).
//   * IMPORT: the triangle soup is read (RWStl/RWObj/RWGltf reader or the hand PLY
//     reader) into a Poly_Triangulation, then SEWN into a shell and (when closed)
//     promoted to a SOLID — the C++ analogue of the Python oracle's "as_solid"
//     conversion. The resulting TopoDS_Shape flows through the SAME import-feature
//     pipeline as a B-rep body (volume / bbox / AICM tessellation all work unchanged),
//     so the C++ core has no separate MeshBody type: a mesh becomes a real B-rep body
//     on import.
//
// Optional/proprietary lanes: 3MF and 3DM compile when their provisioned libraries are present.
// Modern SolidWorks parts/assemblies import portably from saved display tessellations, with
// Windows COM -> STEP preferred for exact analytic B-rep; Parasolid still requires COM.
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include <TopoDS_Shape.hxx>

namespace chudware::doc {

// Detect the format code from a path's extension: "step"/"iges"/"brep"/"stl"/"obj"/
// "ply"/"gltf"/"glb"/"3mf"/"3dm"/"sldprt"/… (mirrors formats.py::_EXT_TO_FORMAT).
// Throws OpError("invalid_op") on an unknown extension.
std::string detect_format(const std::string& path);

// One row of the import/export format matrix (port of formats.py::FormatInfo). The
// host builds its File > Open/Import/Export filters from this via the doc_formats
// RPC, so the UI never lists a format the engine can't handle (and vice-versa).
//   lane: "brep" (OCCT solids) | "mesh" (triangle soup) | "rhino" | "proprietary"
struct FormatInfo {
    std::string fmt;                 // format key, e.g. "step"
    std::string label;               // human label, e.g. "STEP"
    std::vector<std::string> exts;   // extensions incl. leading dot, primary first
    std::string lane;
    bool can_import = true;
    bool can_export = true;
};

// The full format matrix in registry order — a byte-parity port of formats.py::
// FORMAT_REGISTRY (the doc_formats RPC payload source). Single source of truth;
// detect_format's extension map is a projection of these entries.
const std::vector<FormatInfo>& format_registry();

// True iff this machine can COM-automate SolidWorks (SldWorks.Application registered).
// Cheap registry probe; never launches SolidWorks. Always false off Windows.
bool solidworks_available();

// SolidWorks/Parasolid → STEP via SolidWorks COM (port of engine/doc/formats_solidworks.py
// sw_com_export_step). Opens `src_path` (.sldprt/.sldasm/.x_t/.x_b — SolidWorks reads
// Parasolid) invisibly, Save-As STEP to a temp file, closes the doc, and returns the STEP
// path for the normal B-rep import lane. Throws OpError("import_failed") with an actionable
// "Save As STEP" message when SolidWorks/COM is unavailable or the automation fails.
std::string sw_export_step(const std::string& src_path);

// Convert a DWG through LibreDWG's `dwg2dxf` (dwg.h). Public only so the focused native
// boundary test can verify literal argv handling; callers normally use import_bodies.
std::string dwg_to_temp_dxf(const std::string& path);

// Number of ENTITIES-section entities in an ASCII DXF (block definitions excluded). The DWG
// writer uses it to prove a written file re-reads completely.
std::size_t dxf_entity_count(const std::string& path);

// A raw triangle-soup mesh (the C++ analogue of engine/doc/mesh_body.py::MeshBody used
// only internally by the mesh readers/writers — the public body model stays TopoDS).
// `verts` is N*3 doubles (x,y,z row-major); `tris` is M*3 0-based vertex indices.
// Shading normals are NOT carried here: the mesh lane derives smooth per-vertex normals
// from the triangle soup itself (mesh_to_shape, 40 deg crease angle), because the only
// authored normals any reader ever recovered — SolidWorks' saved display strips — measured
// wrong on real parts.
struct MeshData {
    std::vector<double> verts;     // 3*N
    std::vector<uint32_t> tris;    // 3*M (0-based)
    std::size_t vertex_count() const { return verts.size() / 3; }
    std::size_t triangle_count() const { return tris.size() / 3; }
};

// Tessellation quality controls for mesh-format export (STL/OBJ/PLY/3MF/glTF/3DM).
// Defaults reproduce the historical hardcoded behaviour (0.05mm linear / 0.35rad
// angular, absolute deflection, binary STL) so callers that don't set them are
// unchanged.
struct TessOptions {
    double lin_deflection = 0.05;  // max chord deviation; mm (absolute) or a fraction (relative)
    double ang_deflection = 0.35;  // max normal deviation, radians
    bool relative = false;         // scale linear deflection by each edge size (size-robust)
    bool ascii = false;            // STL only: ASCII vs binary container
    int units = 4;                 // dxf/dwg/sat: $INSUNITS code of the written coordinates
};

// Millimetres per unit of a DXF $INSUNITS code (0 = unitless counts as mm). Throws
// OpError("invalid_op") on an unknown code.
double insunits_mm(int code);

// "mm" | "in" | "ft" | "cm" | "m" | "um" | "mil" | "dm" (or a numeric code) -> $INSUNITS code.
// Throws OpError("invalid_op") naming the accepted values.
int parse_units(const std::string& units);

// Tessellate a TopoDS_Shape (solid or shell) into a triangle soup at the given
// tessellation quality (defaults to 0.05mm linear / 0.35rad angular). Used by the mesh
// writers. The existing triangulation is cleared first so the requested deflection is
// honored exactly even if the shape was previously meshed at a different quality.
// Tangent boundary cusps are sampled on their exact curves before face meshing;
// located instances of shared native edges use synchronized parameter samples.
// This prevents overlapping chords without modifying the source B-rep.
MeshData shape_to_mesh(const TopoDS_Shape& shape, const TessOptions& tess = TessOptions{});

// Count the triangles/vertices a mesh export would produce at the given quality, without
// writing a file. Returns {triangles, vertices} summed over the bodies. Used by the GUI
// export dialog's live estimate.
std::pair<std::size_t, std::size_t> estimate_tessellation(
    const std::vector<TopoDS_Shape>& bodies, const TessOptions& tess = TessOptions{});

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

// Build a TopoDS_Shape from a triangle soup. A welded, closed, consistently wound single-
// shell mesh becomes a SOLID directly, one planar face per coplanar triangle set (curved
// regions stay faceted); any other soup is sewn into a shell and promoted to a SOLID when
// the shell is closed (the "as_solid" path). Empty mesh -> a null shape.
TopoDS_Shape mesh_to_shape(const MeshData& mesh);

// Write surviving solid and non-solid bodies to `path` in `fmt`. Supported: step, iges,
// brep, stl, obj, ply, gltf, glb, 3mf, 3dm, dxf, dwg, sat (mesh formats tessellate at `tess`).
// DXF/DWG write solid/shell bodies as editable ACIS 3DSOLID/BODY entities (DXF R2000, DWG
// R2000) and 2D/wire bodies as LINE/ARC/CIRCLE/polylines, one layer per body when there are
// several; SAT writes ACIS 7.0 text. These three write coordinates in `tess.units`.
// Bodies are merged into one compound for the B-rep writers (mirrors formats.py::
// _shapes_to_export_list). Throws OpError("export_failed") on an unsupported format or
// a writer failure. Returns the absolute path written.
std::string export_bodies(const std::vector<TopoDS_Shape>& bodies,
                          const std::string& path, const std::string& fmt,
                          const TessOptions& tess = TessOptions{});

// Read solid and non-solid bodies from `path`. Supported: step (multi-body), iges (sewn+healed),
// brep, mesh formats stl/obj/ply/gltf/glb/3mf/3dm (sewn into a shell/solid),
// dxf/dwg (one 2D profile body of faces + wires, one mesh body per layer of
// 3DFACE/POLYFACE/mesh entities, and one exact B-rep body per ACIS 3DSOLID/BODY/REGION/
// SURFACE entity; INSERT blocks expanded), sat (ACIS .sat/.sab) and
// modern sldprt/sldasm saved-display tessellations. Returns (name, shape) pairs in file
// order; SolidWorks assemblies retain flattened hierarchy/instance path names and world
// transforms. Partial STEP/IGES transfer returns surviving bodies only when `warnings`
// is supplied; otherwise it refuses rather than silently losing source roots.
// `source_units` (optional) receives the file's drawing units as a DXF $INSUNITS code
// (1 inch, 4 mm, ...) for dxf/dwg/sat, 0 when unknown or another format.
// Throws OpError("import_failed") on an unsupported format, malformed stream,
// unavailable optional dependency, or empty result. x_t/x_b must route through COM -> STEP.
std::vector<std::pair<std::string, TopoDS_Shape>> import_bodies(
    const std::string& path, const std::string& fmt,
    std::vector<std::string>* warnings = nullptr, int* source_units = nullptr);

// Keep solid body ordinals stable, then append uncovered non-solid remnants.
// Shapes without solids retain their existing whole-shape grouping.
std::vector<TopoDS_Shape> split_import_bodies(const TopoDS_Shape& shape);

// Serialize a shape to a base64-encoded OCCT BREP blob, and back. Used to BAKE imported
// geometry directly into the .step+ container (document.json) so a saved assembly no
// longer depends on the original source file surviving on disk (Bug N-8). The BREP is the
// kernel's own lossless format, so the round-trip is exact. brep_b64_to_shape returns a
// null shape on a malformed/empty blob.
std::string shape_to_brep_b64(const TopoDS_Shape& shape);
TopoDS_Shape brep_b64_to_shape(const std::string& b64);

}  // namespace chudware::doc
