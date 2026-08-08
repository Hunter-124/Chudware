// hex_mesher.cpp — see hex_mesher.h. Structured hex8 grid for a box/rectangular prism.
#include "sim/hex_mesher.h"

#include "prof/phases.h"  // per-phase timers (StudyScope "mesh_hex")

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

namespace chudware::sim {

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

// See hex_mesher.h. Classifies a STEP solid as a mappable axis-aligned box and reports its
// bbox dimensions. Used by mesh_box_hex (the gate before grid generation) and by the
// Simple-mode auto-pick in mesher.cpp (to choose hex vs tet without duplicating OCCT reads).
BoxGeomInfo detect_box_geometry(const std::string& step_path) {
    BoxGeomInfo info;
    TopoDS_Shape shape;
    try {
        STEPControl_Reader reader;
        if (reader.ReadFile(step_path.c_str()) != IFSelect_RetDone) {
            info.error = "hex: could not read STEP '" + step_path + "'";
            return info;
        }
        reader.TransferRoots();
        shape = reader.OneShape();
    } catch (const std::exception& e) {
        info.error = std::string("hex: STEP read threw: ") + e.what();
        return info;
    }
    return detect_box_geometry(shape);
}

// Shape overload (P4): the classification proper — the path entry point above and the
// once-parsed pipeline in mesh_step both land here, so nobody re-reads the STEP.
BoxGeomInfo detect_box_geometry(const TopoDS_Shape& shape) {
    BoxGeomInfo info;
    if (shape.IsNull()) {
        info.error = "hex: STEP produced no shape";
        return info;
    }

    // bbox from the vertices: exact for a true axis-aligned box (its 8 corners span the full
    // extent). For a curved solid the vertex hull underestimates the extent, but such a solid
    // is rejected by the face/volume checks below, so the dims are only trusted when is_box.
    TopTools_IndexedMapOfShape verts;
    TopExp::MapShapes(shape, TopAbs_VERTEX, verts);
    if (verts.Extent() > 0) {
        double xmin = std::numeric_limits<double>::infinity(), ymin = xmin, zmin = xmin;
        double xmax = -xmin, ymax = -xmin, zmax = -xmin;
        for (int i = 1; i <= verts.Extent(); ++i) {
            const gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(verts(i)));
            xmin = std::min(xmin, p.X()); xmax = std::max(xmax, p.X());
            ymin = std::min(ymin, p.Y()); ymax = std::max(ymax, p.Y());
            zmin = std::min(zmin, p.Z()); zmax = std::max(zmax, p.Z());
        }
        info.Lx = xmax - xmin;
        info.Ly = ymax - ymin;
        info.Lz = zmax - zmin;
        info.diag = std::sqrt(info.Lx * info.Lx + info.Ly * info.Ly + info.Lz * info.Lz);
    }

    // box detection: exactly 6 faces, a real 3D extent, and an actual volume matching the
    // axis-aligned bbox volume (a rotated/curved solid fills less than its bbox -> rejected).
    TopTools_IndexedMapOfShape faces;
    TopExp::MapShapes(shape, TopAbs_FACE, faces);
    if (faces.Extent() != 6) {
        info.error =
            "Hexahedral meshing needs a box/prismatic part (got " +
            std::to_string(faces.Extent()) + " faces); use Tetrahedral for this geometry.";
        return info;
    }
    if (verts.Extent() == 0) {
        info.error = "Hexahedral meshing needs a box/prismatic part; use Tetrahedral.";
        return info;
    }
    if (!(info.Lx > 1e-6 && info.Ly > 1e-6 && info.Lz > 1e-6)) {
        info.error = "Hexahedral meshing needs a 3D box; this part is degenerate/planar.";
        return info;
    }

    double vol = 0.0;
    try {
        GProp_GProps props;
        BRepGProp::VolumeProperties(shape, props);
        vol = std::abs(props.Mass());
    } catch (...) {
        vol = 0.0;
    }
    const double bbox_vol = info.Lx * info.Ly * info.Lz;
    if (vol <= 0.0 || std::abs(vol - bbox_vol) > 1e-3 * bbox_vol) {
        info.error =
            "Hexahedral meshing needs an axis-aligned box; this part isn't a rectangular "
            "prism (rotated/curved). Use Tetrahedral for this geometry.";
        return info;
    }

    info.is_box = true;
    return info;
}

MeshOut mesh_box_hex(const std::string& step_path, const MeshOptions& options) {
    // Path entry point: one STEP parse, then the shape pipeline (P4 — the old code parsed
    // the SAME file twice more inside: once in detect, once for the bbox-min recovery).
    TopoDS_Shape shape;
    try {
        STEPControl_Reader reader;
        if (reader.ReadFile(step_path.c_str()) != IFSelect_RetDone) {
            MeshOut out;
            out.nodes_per_elem = 8;
            out.error = "hex: could not read STEP '" + step_path + "'";
            return out;
        }
        reader.TransferRoots();
        shape = reader.OneShape();
    } catch (const std::exception& e) {
        MeshOut out;
        out.nodes_per_elem = 8;
        out.error = std::string("hex: STEP read threw: ") + e.what();
        return out;
    }
    return mesh_box_hex(shape, options);
}

MeshOut mesh_box_hex(const TopoDS_Shape& shape, const MeshOptions& options) {
    // Per-phase record (prof/phases.h): "detect" (classification) + "generate" (grid).
    chudware::prof::phases::StudyScope _study("mesh_hex");
    MeshOut out;
    out.nodes_per_elem = 8;
    std::string option_error;
    if (!validate_mesh_options(options, option_error)) {
        out.error = "hex: " + option_error;
        return out;
    }
    if (shape.IsNull()) {
        out.error = "hex: STEP produced no shape";
        return out;
    }

    // Classify first: detect_box_geometry runs the 6-face + volume/bbox check. If the part is
    // not a mappable axis-aligned box, surface its error (the caller — mesh_step — falls back
    // to tet meshing when options.fallback_to_tet is set).
    const BoxGeomInfo info = [&] {
        chudware::prof::phases::PhaseScope _p("detect");
        return detect_box_geometry(shape);
    }();
    if (!info.is_box) {
        out.error = info.error;
        return out;
    }
    const double Lx = info.Lx, Ly = info.Ly, Lz = info.Lz;
    chudware::prof::phases::PhaseScope _p_generate("generate");

    // World-space bbox MIN corner for grid placement: the mesh must sit at the CAD part's
    // actual position so FEA fixtures/loads resolve by node coordinate (BoxGeomInfo carries
    // only the dimensions).
    double xmin = 0.0, ymin = 0.0, zmin = 0.0;
    {
        TopTools_IndexedMapOfShape verts;
        TopExp::MapShapes(shape, TopAbs_VERTEX, verts);
        xmin = ymin = zmin = std::numeric_limits<double>::infinity();
        for (int i = 1; i <= verts.Extent(); ++i) {
            const gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(verts(i)));
            xmin = std::min(xmin, p.X());
            ymin = std::min(ymin, p.Y());
            zmin = std::min(zmin, p.Z());
        }
    }
    if (!std::isfinite(xmin) || !std::isfinite(ymin) ||
        !std::isfinite(zmin)) {
        out.error = "hex: box has non-finite bounds";
        return out;
    }

    // --- grid sizing (same fineness model as the tet mesher) -------------------------
    const double fineness = std::min(
        1.0, std::max(0.0, options.density >= 0.0 ? options.density : options.fineness));
    const double diag = std::sqrt(Lx * Lx + Ly * Ly + Lz * Lz);
    const bool explicit_size = options.target_size_mm > 0.0;
    const double base =
        explicit_size ? options.target_size_mm
                      : diag / (8.0 + 52.0 * fineness);
    if (!std::isfinite(base) || !(base > 0.0)) {
        out.error = "hex: computed element size is non-finite or non-positive";
        return out;
    }

    constexpr int kMaxDiv = 800000;
    auto ndiv = [&](double L) {
        const double raw = L / base;
        if (!std::isfinite(raw) || raw > static_cast<double>(kMaxDiv))
            return kMaxDiv;
        return std::max(1, static_cast<int>(std::lround(raw)));
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
    out.mesh_type_used = "hex8";
    out.ok = true;
    mesh_cache_note_generation();
    return out;
}

}  // namespace chudware::sim
