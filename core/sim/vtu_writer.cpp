// vtu_writer.cpp — see sim/vtu_writer.h.
#include "sim/vtu_writer.h"

#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>
#include <system_error>
#include <vector>

namespace chudware::sim {
namespace {

// Chudware mid-edge order {0,1},{1,2},{2,0},{3,0},{3,2},{3,1} mapped onto VTK's
// VTK_QUADRATIC_TETRA order {0,1},{1,2},{2,0},{0,3},{1,3},{2,3}: only the last
// two mid-edge nodes differ, so 8 and 9 swap.
constexpr int kTet10ToVtk[10] = {0, 1, 2, 3, 4, 5, 6, 7, 9, 8};

// %.10g keeps a double round-trippable at display width and never emits
// locale-dependent separators (the stream operator<< would honour the global
// locale, which a host may have changed).
void append_number(std::string &out, double value) {
  char buf[32];
  const int n = std::snprintf(buf, sizeof(buf), "%.10g", value);
  if (n > 0) out.append(buf, static_cast<size_t>(n));
}

void append_int(std::string &out, long long value) {
  char buf[24];
  const int n = std::snprintf(buf, sizeof(buf), "%lld", value);
  if (n > 0) out.append(buf, static_cast<size_t>(n));
}

} // namespace

bool write_vtu(const std::string &path, const std::vector<double> &points,
               const std::vector<int> &tets, int nodes_per_elem,
               const std::vector<VtuPointField> &fields, std::string &error,
               const std::vector<uint16_t>& material_id) {
  error.clear();
  if (path.empty()) {
    error = "vtu: empty output path";
    return false;
  }
  if (nodes_per_elem != 4 && nodes_per_elem != 10) {
    error = "vtu: only tet4 and tet10 meshes can be written (nodes_per_elem=" +
            std::to_string(nodes_per_elem) + ")";
    return false;
  }
  if (points.empty() || points.size() % 3 != 0) {
    error = "vtu: points must be a non-empty n*3 array";
    return false;
  }
  const size_t stride = static_cast<size_t>(nodes_per_elem);
  if (tets.empty() || tets.size() % stride != 0) {
    error = "vtu: connectivity length is not a multiple of nodes_per_elem";
    return false;
  }
  const size_t n_nodes = points.size() / 3;
  const size_t n_cells = tets.size() / stride;
  if (!material_id.empty() && material_id.size() != n_cells) {
    error = "vtu: material_id length must match the cell count";
    return false;
  }
  for (int index : tets) {
    if (index < 0 || static_cast<size_t>(index) >= n_nodes) {
      error = "vtu: connectivity references a node outside the mesh";
      return false;
    }
  }
  for (const VtuPointField &field : fields) {
    if (field.name.empty()) {
      error = "vtu: point field requires a name";
      return false;
    }
    if (field.components != 1 && field.components != 3) {
      error = "vtu: point field '" + field.name +
              "' must have 1 or 3 components";
      return false;
    }
    if (field.values.size() !=
        n_nodes * static_cast<size_t>(field.components)) {
      error = "vtu: point field '" + field.name + "' has " +
              std::to_string(field.values.size()) + " values, expected " +
              std::to_string(n_nodes * static_cast<size_t>(field.components));
      return false;
    }
  }

  try {
    const std::filesystem::path out_path(path);
    if (out_path.has_parent_path() && !out_path.parent_path().empty()) {
      std::error_code ec;
      std::filesystem::create_directories(out_path.parent_path(), ec);
      if (ec && !std::filesystem::is_directory(out_path.parent_path())) {
        error = "vtu: cannot create directory '" +
                out_path.parent_path().string() + "': " + ec.message();
        return false;
      }
    }

    std::string body;
    // Points + connectivity dominate; ~24 chars per number is a close upper
    // bound for %.10g output, so this reserve avoids repeated reallocation on
    // multi-million-node meshes.
    body.reserve(points.size() * 24 + tets.size() * 10 + 1024);

    // Name the first scalar/vector field as the Piece's active array so a
    // viewer colours by something meaningful on open.
    std::string active_scalar, active_vector;
    for (const VtuPointField &field : fields) {
      if (field.components == 3 && active_vector.empty())
        active_vector = field.name;
      if (field.components == 1 && active_scalar.empty())
        active_scalar = field.name;
    }

    body += "<?xml version=\"1.0\"?>\n";
    body += "<VTKFile type=\"UnstructuredGrid\" version=\"0.1\" "
            "byte_order=\"LittleEndian\">\n";
    body += "  <UnstructuredGrid>\n";
    body += "    <Piece NumberOfPoints=\"";
    append_int(body, static_cast<long long>(n_nodes));
    body += "\" NumberOfCells=\"";
    append_int(body, static_cast<long long>(n_cells));
    body += "\">\n";

    body += "      <Points>\n        <DataArray type=\"Float64\" "
            "NumberOfComponents=\"3\" format=\"ascii\">\n";
    for (size_t i = 0; i < n_nodes; ++i) {
      body += "         ";
      for (int k = 0; k < 3; ++k) {
        body += ' ';
        append_number(body, points[i * 3 + static_cast<size_t>(k)]);
      }
      body += '\n';
    }
    body += "        </DataArray>\n      </Points>\n";

    body += "      <Cells>\n        <DataArray type=\"Int32\" "
            "Name=\"connectivity\" format=\"ascii\">\n";
    for (size_t e = 0; e < n_cells; ++e) {
      const int *element = &tets[e * stride];
      body += "         ";
      for (size_t j = 0; j < stride; ++j) {
        body += ' ';
        append_int(body, nodes_per_elem == 10 ? element[kTet10ToVtk[j]]
                                              : element[j]);
      }
      body += '\n';
    }
    body += "        </DataArray>\n        <DataArray type=\"Int32\" "
            "Name=\"offsets\" format=\"ascii\">\n         ";
    for (size_t e = 1; e <= n_cells; ++e) {
      body += ' ';
      append_int(body, static_cast<long long>(e * stride));
      if (e % 20 == 0 && e != n_cells) body += "\n         ";
    }
    body += "\n        </DataArray>\n        <DataArray type=\"UInt8\" "
            "Name=\"types\" format=\"ascii\">\n         ";
    const int cell_type = nodes_per_elem == 10 ? 24 : 10;
    for (size_t e = 0; e < n_cells; ++e) {
      body += ' ';
      append_int(body, cell_type);
      if ((e + 1) % 40 == 0 && e + 1 != n_cells) body += "\n         ";
    }
    body += "\n        </DataArray>\n      </Cells>\n";

    body += "      <PointData";
    if (!active_scalar.empty()) body += " Scalars=\"" + active_scalar + "\"";
    if (!active_vector.empty()) body += " Vectors=\"" + active_vector + "\"";
    body += ">\n";
    for (const VtuPointField &field : fields) {
      body += "        <DataArray type=\"Float64\" Name=\"" + field.name +
              "\" NumberOfComponents=\"";
      append_int(body, field.components);
      body += "\" format=\"ascii\">\n";
      for (size_t i = 0; i < n_nodes; ++i) {
        body += "         ";
        for (int k = 0; k < field.components; ++k) {
          const double value =
              field.values[i * static_cast<size_t>(field.components) +
                           static_cast<size_t>(k)];
          body += ' ';
          // ParaView rejects the XML outright on nan/inf tokens; a missing
          // value is reported as zero rather than a corrupt file.
          append_number(body, std::isfinite(value) ? value : 0.0);
        }
        body += '\n';
      }
      body += "        </DataArray>\n";
    }
    body += "      </PointData>\n";
    if (!material_id.empty()) {
      body += "      <CellData Scalars=\"material_id\">\n"
              "        <DataArray type=\"UInt16\" Name=\"material_id\" format=\"ascii\">\n";
      for (uint16_t id : material_id) {
        body += ' ';
        append_int(body, id);
      }
      body += "\n        </DataArray>\n      </CellData>\n";
    }
    body += "    </Piece>\n  </UnstructuredGrid>\n</VTKFile>\n";

    std::ofstream out(out_path, std::ios::binary | std::ios::trunc);
    if (!out) {
      error = "vtu: cannot open '" + path + "' for writing";
      return false;
    }
    out.write(body.data(), static_cast<std::streamsize>(body.size()));
    out.flush();
    if (!out) {
      error = "vtu: write to '" + path + "' failed";
      return false;
    }
    return true;
  } catch (const std::exception &e) {
    error = std::string("vtu: ") + e.what();
    return false;
  } catch (...) {
    error = "vtu: unknown failure";
    return false;
  }
}

} // namespace chudware::sim
