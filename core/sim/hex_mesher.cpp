// hex_mesher.cpp — see hex_mesher.h. Structured hex8 grid for a box/rectangular prism.
#include "sim/hex_mesher.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>

#include <BRepGProp.hxx>
#include <BRep_Tool.hxx>
#include <GProp_GProps.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Reader.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Vertex.hxx>
#include <gp_Pnt.hxx>

namespace aicad::sim {

namespace {

// Fill the MeshOut quality fields for a regular hex grid. A box cell is orthogonal, so
// every element is identical: scaled Jacobian == 1.0 and aspect == max/min edge length.
void fill_hex_quality(MeshOut& out, double dx, double dy, double dz) {
    out.aspect_hist.assign(10, 0);
    const double lo = std::min({dx, dy, dz});
    const double hi = std::max({dx, dy, dz});
    const double aspect = (lo > 0.0) ? hi / lo : 1.0;
    out.aspect_min = out.aspect_avg = out.aspect_max = aspect;
    out.jacobian_min = 1.0;  // orthogonal cells
    out.pct_poor = (aspect > kPoorAspect) ? 100.0 : 0.0;
    int b = static_cast<int>((aspect - 1.0) / (kPoorAspect - 1.0) * 10.0);
    if (b < 0) b = 0;
    if (b > 9) b = 9;
    if (out.n_elems > 0) out.aspect_hist[static_cast<size_t>(b)] = out.n_elems;
    out.min_tet_volume_mm3 = dx * dy * dz;  // here: the (uniform) cell volume
    out.max_tet_edge_ratio = aspect;
}

}  // namespace

MeshOut mesh_box_hex(const std::string& step_path, const MeshOptions& options) {
    MeshOut out;
    out.nodes_per_elem = 8;

    // --- read the STEP -------------------------------------------------------------
    TopoDS_Shape shape;
    try {
        STEPControl_Reader reader;
        if (reader.ReadFile(step_path.c_str()) != IFSelect_RetDone) {
            out.error = "hex: could not read STEP '" + step_path + "'";
            return out;
        }
        reader.TransferRoots();
        shape = reader.OneShape();
    } catch (const std::exception& e) {
        out.error = std::string("hex: STEP read threw: ") + e.what();
        return out;
    }
    if (shape.IsNull()) {
        out.error = "hex: STEP produced no shape";
        return out;
    }

    // --- box detection: 6 faces, axis-aligned bbox from the vertices, volume match ---
    TopTools_IndexedMapOfShape faces;
    TopExp::MapShapes(shape, TopAbs_FACE, faces);
    if (faces.Extent() != 6) {
        out.error =
            "Hexahedral meshing needs a box/prismatic part (got " +
            std::to_string(faces.Extent()) + " faces); use Tetrahedral for this geometry.";
        return out;
    }

    TopTools_IndexedMapOfShape verts;
    TopExp::MapShapes(shape, TopAbs_VERTEX, verts);
    if (verts.Extent() == 0) {
        out.error = "Hexahedral meshing needs a box/prismatic part; use Tetrahedral.";
        return out;
    }
    double xmin = std::numeric_limits<double>::infinity(), ymin = xmin, zmin = xmin;
    double xmax = -xmin, ymax = -xmin, zmax = -xmin;
    for (int i = 1; i <= verts.Extent(); ++i) {
        const gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(verts(i)));
        xmin = std::min(xmin, p.X()); xmax = std::max(xmax, p.X());
        ymin = std::min(ymin, p.Y()); ymax = std::max(ymax, p.Y());
        zmin = std::min(zmin, p.Z()); zmax = std::max(zmax, p.Z());
    }
    const double Lx = xmax - xmin, Ly = ymax - ymin, Lz = zmax - zmin;
    if (!(Lx > 1e-6 && Ly > 1e-6 && Lz > 1e-6)) {
        out.error = "Hexahedral meshing needs a 3D box; this part is degenerate/planar.";
        return out;
    }

    // A rotated or non-prismatic solid fills less than its axis-aligned bbox: require the
    // actual volume to match the bbox volume (so this is a true axis-aligned rectangular
    // prism). A 45-degree-rotated box fails here and the caller falls back to tets.
    double vol = 0.0;
    try {
        GProp_GProps props;
        BRepGProp::VolumeProperties(shape, props);
        vol = std::abs(props.Mass());
    } catch (...) {
        vol = 0.0;
    }
    const double bbox_vol = Lx * Ly * Lz;
    if (vol <= 0.0 || std::abs(vol - bbox_vol) > 1e-3 * bbox_vol) {
        out.error =
            "Hexahedral meshing needs an axis-aligned box; this part isn't a rectangular "
            "prism (rotated/curved). Use Tetrahedral for this geometry.";
        return out;
    }

    // --- grid sizing (same fineness model as the tet mesher) -------------------------
    const double fineness = std::min(
        1.0, std::max(0.0, options.density >= 0.0 ? options.density : options.fineness));
    const double diag = std::sqrt(Lx * Lx + Ly * Ly + Lz * Lz);
    const bool explicit_size =
        std::isfinite(options.target_size_mm) && options.target_size_mm > 0.0;
    const double base = explicit_size ? options.target_size_mm : diag / (8.0 + 52.0 * fineness);

    auto ndiv = [&](double L) {
        int n = static_cast<int>(std::lround(L / base));
        return std::max(1, n);
    };
    int nx = ndiv(Lx), ny = ndiv(Ly), nz = ndiv(Lz);

    // Cap total element count so a fine setting on a big part can't explode memory.
    constexpr long long kMaxHex = 800000;
    long long total = static_cast<long long>(nx) * ny * nz;
    if (total > kMaxHex) {
        const double s = std::cbrt(static_cast<double>(kMaxHex) / static_cast<double>(total));
        nx = std::max(1, static_cast<int>(nx * s));
        ny = std::max(1, static_cast<int>(ny * s));
        nz = std::max(1, static_cast<int>(nz * s));
    }
    const double dx = Lx / nx, dy = Ly / ny, dz = Lz / nz;

    // --- nodes: (nx+1)(ny+1)(nz+1) lattice ------------------------------------------
    const int snx = nx + 1, sny = ny + 1, snz = nz + 1;
    auto nid = [&](int i, int j, int k) { return i + snx * (j + sny * k); };
    out.points.reserve(static_cast<size_t>(snx) * sny * snz * 3);
    for (int k = 0; k < snz; ++k)
        for (int j = 0; j < sny; ++j)
            for (int i = 0; i < snx; ++i) {
                out.points.push_back(xmin + i * dx);
                out.points.push_back(ymin + j * dy);
                out.points.push_back(zmin + k * dz);
            }

    // --- hex8 elements: standard FEM corner order (bottom 0-3 CCW, top 4-7) ----------
    out.tets.reserve(static_cast<size_t>(nx) * ny * nz * 8);
    for (int k = 0; k < nz; ++k)
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                out.tets.push_back(nid(i, j, k));
                out.tets.push_back(nid(i + 1, j, k));
                out.tets.push_back(nid(i + 1, j + 1, k));
                out.tets.push_back(nid(i, j + 1, k));
                out.tets.push_back(nid(i, j, k + 1));
                out.tets.push_back(nid(i + 1, j, k + 1));
                out.tets.push_back(nid(i + 1, j + 1, k + 1));
                out.tets.push_back(nid(i, j + 1, k + 1));
            }

    out.char_len_mm = base;
    out.n_nodes = static_cast<int>(out.points.size() / 3);
    out.n_elems = static_cast<int>(out.tets.size() / 8);
    fill_hex_quality(out, dx, dy, dz);
    out.ok = true;
    return out;
}

}  // namespace aicad::sim
