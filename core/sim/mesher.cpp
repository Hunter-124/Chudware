// mesher.cpp — see mesher.h. C++ port of engine/sim/mesher.py.
//
// Mirrors the Python oracle's EXACT gmsh sequence so the tet mesh is bit-identical:
//   initialize -> set Verbosity=1 -> model.add -> occ.importShapes -> occ.synchronize
//   -> getBoundingBox -> char_len from min non-zero side -> CharacteristicLengthMin/Max
//   -> ElementOrder=1 -> Algorithm3D=1 (Delaunay) -> mesh.generate(3) -> write(.msh)
//   -> getNodes -> tag->0-based remap -> getElements(dim=3), tet4 (etype==4) only.
// NO other options are set (defaults must match Python's wheel for identical output).
//
// IMPLEMENTATION NOTE — we use the C API (gmshc.h), NOT the C++ API (gmsh.h):
// the official prebuilt Windows SDK's libgmsh is built with MinGW/GCC, so its C++
// symbols carry Itanium (GCC) name mangling that MSVC's import resolver can never
// match (verified: dumpbin shows `_ZN4gmsh...`). The `extern "C"` C API
// (gmshInitialize/...) is ABI-stable across compilers and links cleanly from MSVC.
// The Python oracle's `gmsh` wheel calls these SAME C entry points (lib/gmsh.py ->
// ctypes lib.gmshInitialize ...), so this is the identical code path, not a reimpl.

#include "sim/mesher.h"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <filesystem>
#include <unordered_map>

// gmshc.h is a pure-C header with NO `extern "C"` guard of its own — wrap it so the
// C++ compiler emits C linkage (the DLL exports plain C names: gmshInitialize, …).
extern "C" {
#include <gmshc.h>
}

namespace aicad::sim {

namespace {

// RAII guard so gmshFinalize() runs on every exit path — mirrors Python's
// try/finally: finalize() that always runs even when generate() raises.
struct GmshGuard {
    ~GmshGuard() {
        int ierr = 0;
        gmshFinalize(&ierr);  // best-effort; never throw out of a destructor
    }
};

}  // namespace

MeshOut mesh_step(const std::string& step_path, double density) {
    MeshOut out;

    // Clip density to [0, 1] (float(np.clip(density, 0.0, 1.0))).
    density = std::min(1.0, std::max(0.0, density));

    // Derive a .msh output path next to the step file (Path.with_suffix(".msh")).
    std::filesystem::path mesh_path_fs = std::filesystem::path(step_path);
    mesh_path_fs.replace_extension(".msh");
    const std::string mesh_path = mesh_path_fs.string();

    int ierr = 0;

    // gmsh.initialize(): argc=0, argv=NULL, readConfigFiles=1, run=0 (the wheel's
    // gmsh.initialize() defaults — lib/gmsh.py passes exactly these).
    gmshInitialize(0, nullptr, 1, 0, &ierr);
    if (ierr != 0) {
        out.ok = false;
        out.error = "gmsh: initialize failed";
        return out;
    }
    GmshGuard guard;  // gmshFinalize() on any return past this point

    auto fail = [&](const std::string& msg) -> MeshOut {
        out.ok = false;
        out.error = "gmsh: " + msg;
        return out;
    };

    gmshOptionSetNumber("General.Verbosity", 1, &ierr);
    if (ierr != 0) return fail("setNumber(Verbosity)");

    gmshModelAdd("model", &ierr);
    if (ierr != 0) return fail("model.add");

    int* out_dim_tags = nullptr;
    std::size_t out_dim_tags_n = 0;
    // highestDimOnly=1, format="" — the C++/Python default (highestDimOnly=true).
    gmshModelOccImportShapes(step_path.c_str(), &out_dim_tags, &out_dim_tags_n, 1, "",
                             &ierr);
    if (out_dim_tags) gmshFree(out_dim_tags);
    if (ierr != 0) return fail("occ.importShapes (is the STEP path valid?)");

    gmshModelOccSynchronize(&ierr);
    if (ierr != 0) return fail("occ.synchronize");

    // Bounding box -> characteristic length.
    double xmin, ymin, zmin, xmax, ymax, zmax;
    gmshModelGetBoundingBox(-1, -1, &xmin, &ymin, &zmin, &xmax, &ymax, &zmax, &ierr);
    if (ierr != 0) return fail("getBoundingBox");
    const double side_lens[3] = {std::fabs(xmax - xmin), std::fabs(ymax - ymin),
                                 std::fabs(zmax - zmin)};
    // min(d for d in side_lens if d > 1e-6).
    double min_dim = 0.0;
    bool have_min = false;
    for (double d : side_lens) {
        if (d > 1e-6) {
            if (!have_min || d < min_dim) min_dim = d;
            have_min = true;
        }
    }
    if (!have_min) return fail("degenerate bounding box (no side dimension > 1e-6)");

    // lerp: density=0 -> 0.25*min_dim, density=1 -> 0.05*min_dim.
    const double char_len = min_dim * (0.25 - 0.20 * density);

    gmshOptionSetNumber("Mesh.CharacteristicLengthMin", char_len * 0.1, &ierr);
    if (ierr != 0) return fail("setNumber(CharacteristicLengthMin)");
    gmshOptionSetNumber("Mesh.CharacteristicLengthMax", char_len, &ierr);
    if (ierr != 0) return fail("setNumber(CharacteristicLengthMax)");
    gmshOptionSetNumber("Mesh.ElementOrder", 1, &ierr);
    if (ierr != 0) return fail("setNumber(ElementOrder)");
    gmshOptionSetNumber("Mesh.Algorithm3D", 1, &ierr);  // Delaunay 3D
    if (ierr != 0) return fail("setNumber(Algorithm3D)");

    gmshModelMeshGenerate(3, &ierr);
    if (ierr != 0) return fail("mesh.generate(3)");
    gmshWrite(mesh_path.c_str(), &ierr);
    if (ierr != 0) return fail("write(.msh)");

    // Extract nodes (coords flat -> reshape (-1,3)).
    std::size_t* node_tags = nullptr;
    std::size_t node_tags_n = 0;
    double* coord = nullptr;
    std::size_t coord_n = 0;
    double* param_coord = nullptr;
    std::size_t param_coord_n = 0;
    gmshModelMeshGetNodes(&node_tags, &node_tags_n, &coord, &coord_n, &param_coord,
                          &param_coord_n, -1, -1, 0, 1, &ierr);
    if (ierr != 0) {
        if (node_tags) gmshFree(node_tags);
        if (coord) gmshFree(coord);
        if (param_coord) gmshFree(param_coord);
        return fail("mesh.getNodes");
    }
    out.points.assign(coord, coord + coord_n);  // flat n*3 row-major float64

    // Contiguous tag -> 0-based index map (gmsh tags may not be 0-based).
    std::unordered_map<std::size_t, int> tag_to_idx;
    tag_to_idx.reserve(node_tags_n * 2);
    for (std::size_t i = 0; i < node_tags_n; ++i) {
        tag_to_idx[node_tags[i]] = static_cast<int>(i);
    }
    if (node_tags) gmshFree(node_tags);
    if (coord) gmshFree(coord);
    if (param_coord) gmshFree(param_coord);

    // Elements of dim 3; keep only tet4 (element type 4).
    int* elem_types = nullptr;
    std::size_t elem_types_n = 0;
    std::size_t** elem_tags = nullptr;        // [block][i]
    std::size_t* elem_tags_n = nullptr;       // length of each block
    std::size_t elem_tags_nn = 0;             // number of blocks
    std::size_t** elem_node_tags = nullptr;   // [block][i]
    std::size_t* elem_node_tags_n = nullptr;  // length of each block
    std::size_t elem_node_tags_nn = 0;        // number of blocks
    gmshModelMeshGetElements(&elem_types, &elem_types_n, &elem_tags, &elem_tags_n,
                             &elem_tags_nn, &elem_node_tags, &elem_node_tags_n,
                             &elem_node_tags_nn, 3, -1, &ierr);

    bool any_tet = false;
    if (ierr == 0) {
        for (std::size_t b = 0; b < elem_types_n; ++b) {
            if (elem_types[b] != 4) continue;  // tet4 only
            any_tet = true;
            const std::size_t* entags = elem_node_tags[b];
            const std::size_t cnt = elem_node_tags_n[b];
            out.tets.reserve(out.tets.size() + cnt);
            for (std::size_t t = 0; t < cnt; ++t) {
                out.tets.push_back(tag_to_idx.at(entags[t]));
            }
        }
    }

    // Free the nested C-API arrays (each inner pointer + each outer pointer).
    if (elem_types) gmshFree(elem_types);
    if (elem_tags) {
        for (std::size_t b = 0; b < elem_tags_nn; ++b)
            if (elem_tags[b]) gmshFree(elem_tags[b]);
        gmshFree(elem_tags);
    }
    if (elem_tags_n) gmshFree(elem_tags_n);
    if (elem_node_tags) {
        for (std::size_t b = 0; b < elem_node_tags_nn; ++b)
            if (elem_node_tags[b]) gmshFree(elem_node_tags[b]);
        gmshFree(elem_node_tags);
    }
    if (elem_node_tags_n) gmshFree(elem_node_tags_n);

    if (ierr != 0) return fail("mesh.getElements");

    if (!any_tet) {
        out.ok = false;
        out.error = "gmsh produced no tet4 elements for '" + step_path +
                    "'. Check that the geometry is a valid closed solid.";
        return out;
    }

    out.char_len_mm = char_len;
    out.n_nodes = static_cast<int>(out.points.size() / 3);
    out.n_elems = static_cast<int>(out.tets.size() / 4);
    out.ok = true;
    return out;
}

}  // namespace aicad::sim
