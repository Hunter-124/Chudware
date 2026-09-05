// VTK XML UnstructuredGrid (.vtu) writer for simulation results.
//
// An evidence packet for an FEA run has to be independently inspectable: a
// reviewer opens the .vtu in ParaView (or reads it with meshio / a plain XML
// parser) and sees the SAME mesh and the SAME nodal fields the result JSON
// summarised. This writer is therefore deliberately dumb — ASCII, one Piece,
// no compression, no appended binary — so the file is diffable and parseable
// without VTK itself.
//
// Cells are tetrahedra only: tet4 -> VTK_TETRA (10), tet10 ->
// VTK_QUADRATIC_TETRA (24). Chudware's canonical mid-edge order is
// {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}; VTK's is {0,1},{1,2},{2,0},{0,3},{1,3},
// {2,3}, so the last two mid-edge nodes are swapped on write (see kTet10ToVtk).
// Coordinates are millimetres, matching every other Chudware sim surface.
#pragma once

#include <string>
#include <vector>

namespace chudware::sim {

// One point-data array. `components` is 1 (scalar) or 3 (vector); `values` is
// n_nodes * components, node-major.
struct VtuPointField {
  std::string name;
  int components = 1;
  std::vector<double> values;
};

// Write `path` as a VTK XML UnstructuredGrid. Parent directories are created.
// Never throws: every failure returns false with a diagnostic in `error`.
bool write_vtu(const std::string &path, const std::vector<double> &points,
               const std::vector<int> &tets, int nodes_per_elem,
               const std::vector<VtuPointField> &fields, std::string &error);

} // namespace chudware::sim
