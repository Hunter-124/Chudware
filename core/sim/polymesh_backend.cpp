// Polymesh (Polyhedral-FEA) mesher backend. See sim/polymesh_backend.h for why a second
// mesher exists and for the unit convention.
#include "sim/polymesh_backend.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

#include "sim/mesher.h"

namespace chudware::sim {

MesherBackend mesher_backend_from_string(const std::string& name) {
    return name == "polymesh" ? MesherBackend::Polymesh : MesherBackend::Netgen;
}

const char* mesher_backend_name(MesherBackend backend) {
    return backend == MesherBackend::Polymesh ? "polymesh" : "netgen";
}

bool mesher_backend_available(MesherBackend backend) {
#ifdef CHUDWARE_WITH_POLYMESH
    (void)backend;
    return true;
#else
    return backend != MesherBackend::Polymesh;
#endif
}

}  // namespace chudware::sim

#ifndef CHUDWARE_WITH_POLYMESH

namespace chudware::sim {

MeshOut mesh_step_polymesh(const std::string&, const MeshOptions&) {
    MeshOut out;
    out.ok = false;
    out.error =
        "polymesh mesher unavailable: this build was configured without "
        "CHUDWARE_WITH_POLYMESH (reconfigure core with -DCHUDWARE_WITH_POLYMESH=ON)";
    return out;
}

}  // namespace chudware::sim

#else

#include <TopoDS_Shape.hxx>

#include "sim/mesh_size_band.h"
#include "sim/solver_threads.h"

#include <fea/backend.hpp>
#include <fea/nodal_mesh.hpp>
#include <fea/p_elevate.hpp>
#include <pipeline/scene.hpp>

namespace chudware::sim {
namespace {

namespace pm = polymesh;

// polymesh reads OpenMP thread count once from the environment at process start. Chudware
// governs solver/mesher parallelism centrally so a background study cannot starve the GUI
// render thread, so re-point polymesh at the same cap. Runs once per process.
void init_polymesh_runtime() {
    static std::once_flag once;
    std::call_once(once, [] {
        pm::fea::init_runtime_performance();
        const int threads = static_cast<int>(governed_solver_threads());
        if (threads > 0) pm::fea::set_openmp_threads(threads);
    });
}

// Chudware's tet10 slots follow the gmsh MSH_TET_10 convention (mesher.h): mid-edge nodes
// on {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}. polymesh orders its mids on
// (0,1),(1,2),(0,2),(0,3),(1,3),(2,3) (fea/nodal_mesh.hpp). The first four agree; the last
// two are swapped. Getting this wrong does not crash — it silently produces a warped
// quadratic element and a wrong stress field — so it is a named table, not an inline index.
constexpr int kTet10PolymeshToChudware[6] = {4, 5, 6, 7, 9, 8};

void append_note(std::string& note, const std::string& text) {
    if (text.empty()) return;
    note = note.empty() ? text : note + "; " + text;
}

// Options that describe Netgen-only machinery. Reported rather than silently dropped: a
// user who ticked "curvature refinement" and got a uniform mesh deserves to be told why.
std::string inapplicable_options_note(const MeshOptions& options) {
    std::vector<std::string> ignored;
    if (options.curvature) ignored.emplace_back("curvature refinement");
    if (options.structured) ignored.emplace_back("structured hex");
    if (options.quad_surface) ignored.emplace_back("quad-dominated surface");
    if (options.auto_type) ignored.emplace_back("geometry auto-pick");
    if (ignored.empty()) return std::string();
    std::string list = ignored.front();
    for (size_t i = 1; i < ignored.size(); ++i) list += ", " + ignored[i];
    return "polymesh backend does not implement: " + list +
           " (Netgen-only; mesh is a graded Cartesian fill)";
}

}  // namespace

MeshOut mesh_step_polymesh(const std::string& step_path, const MeshOptions& options) {
    MeshOut out;
    if (!validate_mesh_options(options, out.error)) {
        out.ok = false;
        return out;
    }
    init_polymesh_runtime();

    try {
        pm::pipeline::Model model = pm::pipeline::Model::load(step_path);
        if (model.surface.vertices.empty() || model.surface.triangles.empty()) {
            out.ok = false;
            out.error = "polymesh: STEP produced no tessellated surface to fill";
            return out;
        }

        // Size the mesh with Chudware's shared brain, not polymesh's resolve_mesh_size, so
        // one fineness setting means one element size across backends AND the host-memory
        // governor still applies. resolve_size_band needs the OCC shape; polymesh already
        // holds it behind CadModel::shape_handle().
        SizeBand band;
        if (model.cad && model.cad->shape_handle() != nullptr) {
            const TopoDS_Shape& shape =
                *static_cast<const TopoDS_Shape*>(model.cad->shape_handle());
            band = resolve_size_band(shape, options);
        }
        if (!band.ok) {
            out.ok = false;
            out.error = band.error.empty()
                            ? "polymesh: could not resolve element sizing from the STEP"
                            : band.error;
            return out;
        }
        append_note(out.note, band.note);
        append_note(out.note, inapplicable_options_note(options));

        // Ceilings: polymesh refuses rather than thrashing when a fill would exceed them.
        // resolve_size_band has already applied the governor to `base`, so pass polymesh's
        // own defaults as a backstop only.
        const pm::pipeline::ResolvedMeshSize resolved =
            pm::pipeline::resolve_mesh_size(model, band.base);

        // kGradedTet: pure tets (the fixed-stride contract every Chudware solver assumes)
        // with a refined boundary skin, which is the closest polymesh family to what the
        // Netgen path delivers. The hybrid/VEM families emit hex/prism/pyramid/polyhedral
        // cells that MeshOut cannot express, so they are deliberately not selectable here.
        const pm::pipeline::VolumeMeshOutput volume = pm::pipeline::volume_mesh(
            model, band.base, pm::pipeline::VolumeMesher::kGradedTet,
            /*skin_layers=*/2, /*feature_refine=*/true, /*refine_seeds=*/{},
            /*seed_band=*/0.0, /*element_tendency=*/0.0, resolved.element_ceiling,
            resolved.dof_ceiling);

        pm::fea::NodalMesh mesh = volume.mesh;
        if (options.order == 2) {
            mesh = pm::fea::promote_to_quadratic(mesh);
            // Straight-edge mids are a poor approximation on a fillet or a bore. Project
            // the free-surface mids onto the exact BRep, exactly as the Netgen second-order
            // path does with Ng_OCC_Generate_SecondOrder.
            if (model.cad) {
                std::vector<pm::mesh::BoundarySupport> provenance;
                pm::mesh::BoundaryProjectionContext projection;
                if (pm::pipeline::make_boundary_projection(*model.cad, band.base, &projection,
                                                           &provenance)) {
                    pm::pipeline::project_quadratic_boundary_mids(mesh, *model.cad, &projection,
                                                                  band.base);
                }
            }
        }
        mesh.compact_unused_nodes();

        const int stride = options.order == 2 ? 10 : 4;
        const pm::fea::ElementType want =
            options.order == 2 ? pm::fea::ElementType::kTet10 : pm::fea::ElementType::kTet4;
        // A mixed cell here would be silently misread as a tet by every downstream reader
        // (they index a flat array by a constant stride), so refuse instead of truncating.
        for (const pm::fea::NodalElement& element : mesh.elements) {
            if (element.type != want) {
                out.ok = false;
                out.error = std::string("polymesh: mesh contains a ") +
                            pm::fea::element_type_name(element.type) +
                            " element, but Chudware's mesh contract carries a single "
                            "element type (tet4/tet10) — this geometry needs the Netgen "
                            "backend";
                return out;
            }
        }
        if (mesh.nodes.empty() || mesh.elements.empty()) {
            out.ok = false;
            out.error = "polymesh: fill produced an empty mesh (geometry not watertight?)";
            return out;
        }

        out.points.resize(mesh.nodes.size() * 3);
        for (size_t i = 0; i < mesh.nodes.size(); ++i) {
            out.points[3 * i + 0] = mesh.nodes[i].x();
            out.points[3 * i + 1] = mesh.nodes[i].y();
            out.points[3 * i + 2] = mesh.nodes[i].z();
        }
        out.tets.resize(mesh.elements.size() * static_cast<size_t>(stride));
        for (size_t e = 0; e < mesh.elements.size(); ++e) {
            const std::vector<uint32_t>& nodes = mesh.elements[e].nodes;
            if (nodes.size() != static_cast<size_t>(stride)) {
                out.ok = false;
                out.error = "polymesh: element node count does not match its element type";
                return out;
            }
            int* dst = out.tets.data() + e * static_cast<size_t>(stride);
            for (int c = 0; c < 4; ++c) dst[c] = static_cast<int>(nodes[static_cast<size_t>(c)]);
            for (int m = 0; m < stride - 4; ++m)
                dst[kTet10PolymeshToChudware[m]] = static_cast<int>(nodes[static_cast<size_t>(4 + m)]);
        }

        out.char_len_mm = band.base;
        out.n_nodes = static_cast<int>(mesh.nodes.size());
        out.n_elems = static_cast<int>(mesh.elements.size());
        out.nodes_per_elem = stride;
        out.mesh_type_used = stride == 10 ? "tet10-polymesh" : "tet4-polymesh";
        append_note(out.note, volume.mesher_note);
        if (!detail::fill_quality(out)) {
            out.ok = false;  // fill_quality set the diagnostic
            return out;
        }
        mesh_cache_note_generation();
        out.ok = true;
        return out;
    } catch (const std::exception& e) {
        out.ok = false;
        out.error = std::string("polymesh mesher failed: ") + e.what();
        return out;
    } catch (...) {
        out.ok = false;
        out.error = "polymesh mesher failed with an unknown exception";
        return out;
    }
}

}  // namespace chudware::sim

#endif  // CHUDWARE_WITH_POLYMESH
