// VTK XML UnstructuredGrid (.vtu) writer for simulation results.
//
// An evidence packet for an FEA run has to be independently inspectable: a
// reviewer opens the .vtu in ParaView (or reads it with meshio / a plain XML
// parser) and sees the SAME mesh and the SAME nodal fields the result JSON
// summarised. This writer is therefore deliberately dumb — ASCII, one Piece,
// no compression, no appended binary — so the file is diffable and parseable
// without VTK itself.
//
// "The SAME nodal fields the result JSON summarised" is load-bearing and is more
// than shipping the array: an FEA summary scalar may be a MASKED reduction of the
// field (the static solver drops nodes inside two element rings of an ideal fixed
// constraint, whose stress is a clamp singularity). A file carrying only the field
// then peaks well above the reported peak — 403 MPa against a reported 110 on a
// real released case — and nothing in the file explains it. So a static result's
// point data carries `von_mises` AND the `summary_node` 1/0 mask that selected the
// reported peak: threshold summary_node == 1 and the maximum of von_mises over what
// remains IS summary.max_von_mises_mpa, while the unmasked maximum IS
// summary.max_von_mises_raw_mpa. Any new masked/robust scalar must ship its
// selector the same way.
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
