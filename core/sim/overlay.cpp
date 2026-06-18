#include "sim/overlay.h"

#include <atomic>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <limits>
#include <string>
#include <vector>

#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Reader.hxx>
#include <TopoDS_Shape.hxx>

#include "geom/aicm.h"
#include "geom/tessellate.h"

namespace aicad::sim {

namespace {

// Read a STEP file to a single TopoDS_Shape (OneShape over all transferred roots),
// mirroring build123d's import_step + .wrapped used by overlay.tessellate_step_surface.
// Returns a null shape on any failure.
TopoDS_Shape read_step_shape(const std::string& step_path) {
    try {
        STEPControl_Reader r;
        if (r.ReadFile(step_path.c_str()) != IFSelect_RetDone) return TopoDS_Shape();
        r.TransferRoots();
        return r.OneShape();
    } catch (...) {
        return TopoDS_Shape();
    }
}

// Tessellate the STEP surface into an AICM body (body_id 1, "Body1", default color)
// exactly as overlay.tessellate_step_surface. Null shape -> empty body.
aicm::AicmBody tessellate_step_surface(const std::string& step_path) {
    TopoDS_Shape shape = read_step_shape(step_path);
    const float color[3] = {0.71f, 0.76f, 0.82f};
    if (shape.IsNull()) {
        aicm::AicmBody empty;
        empty.body_id = 1;
        empty.name = "Body1";
        empty.color[0] = color[0];
        empty.color[1] = color[1];
        empty.color[2] = color[2];
        return empty;
    }
    return geom::tessellate_body(shape, 1, "Body1", color);
}

// (n_verts*3) surface vertex positions (float pos -> double), like _surface_verts.
std::vector<double> surface_verts(const aicm::AicmBody& bt) {
    std::vector<double> out(bt.pos.size());
    for (size_t i = 0; i < bt.pos.size(); ++i) out[i] = static_cast<double>(bt.pos[i]);
    return out;
}

// Unique temp .aicm path (counter + temp dir; avoids any clock dependency).
std::string temp_aicm_path() {
    static std::atomic<unsigned long long> seq{0};
    unsigned long long n = seq.fetch_add(1);
    std::filesystem::path dir = std::filesystem::temp_directory_path();
    std::filesystem::path p = dir / ("aicad_overlay_" + std::to_string(n) + ".aicm");
    return p.string();
}

// double -> float vector (chunk payloads are float32, like the np.float32 cast in
// scalar_chunk/disp_chunk).
std::vector<float> to_f32(const std::vector<double>& v) {
    std::vector<float> out(v.size());
    for (size_t i = 0; i < v.size(); ++i) out[i] = static_cast<float>(v[i]);
    return out;
}

double field_max(const std::vector<double>& v) {
    double mx = -std::numeric_limits<double>::infinity();
    for (double x : v) if (x > mx) mx = x;
    return std::isfinite(mx) ? mx : 0.0;
}

// bbox diagonal of surface verts (||max - min||), 1.0 if empty — like overlay.py.
double bbox_diag(const std::vector<double>& surf) {
    const size_t m = surf.size() / 3;
    if (m == 0) return 1.0;
    double lo[3], hi[3];
    for (int k = 0; k < 3; ++k) {
        lo[k] = std::numeric_limits<double>::max();
        hi[k] = -std::numeric_limits<double>::max();
    }
    for (size_t i = 0; i < m; ++i)
        for (int k = 0; k < 3; ++k) {
            double c = surf[3 * i + k];
            if (c < lo[k]) lo[k] = c;
            if (c > hi[k]) hi[k] = c;
        }
    double dx = hi[0] - lo[0], dy = hi[1] - lo[1], dz = hi[2] - lo[2];
    return std::sqrt(dx * dx + dy * dy + dz * dz);
}

}  // namespace

std::vector<double> map_to_surface(const std::vector<double>& surf_xyz,
                                   const std::vector<double>& vol_xyz,
                                   const std::vector<double>& vol_values, int dim) {
    const size_t m = surf_xyz.size() / 3;
    const size_t n = vol_xyz.size() / 3;
    std::vector<double> out(m * static_cast<size_t>(dim), 0.0);
    if (n == 0) return out;
    for (size_t i = 0; i < m; ++i) {
        const double sx = surf_xyz[3 * i + 0];
        const double sy = surf_xyz[3 * i + 1];
        const double sz = surf_xyz[3 * i + 2];
        // Nearest volume node, first-minimum tie-break (np.argmin returns first min).
        double best = std::numeric_limits<double>::infinity();
        size_t best_j = 0;
        for (size_t j = 0; j < n; ++j) {
            const double dx = sx - vol_xyz[3 * j + 0];
            const double dy = sy - vol_xyz[3 * j + 1];
            const double dz = sz - vol_xyz[3 * j + 2];
            const double d2 = dx * dx + dy * dy + dz * dz;
            if (d2 < best) {
                best = d2;
                best_j = j;
            }
        }
        for (int k = 0; k < dim; ++k)
            out[i * static_cast<size_t>(dim) + k] =
                vol_values[best_j * static_cast<size_t>(dim) + k];
    }
    return out;
}

std::string static_overlay(const std::string& step_path,
                           const std::vector<double>& vol_points,
                           const std::vector<double>& von_mises,
                           const std::vector<double>& disp_mag,
                           const std::vector<double>& displacement,
                           double yield_mpa, const nlohmann::json& summary) {
    try {
        aicm::AicmBody bt = tessellate_step_surface(step_path);
        std::vector<double> surf = surface_verts(bt);

        std::vector<double> vm_s = map_to_surface(surf, vol_points, von_mises, 1);
        std::vector<double> dm_s = map_to_surface(surf, vol_points, disp_mag, 1);
        std::vector<double> dvec_s = map_to_surface(surf, vol_points, displacement, 3);

        // fos = clip(yield/vm where vm>1e-9 else 15, 0..15).
        std::vector<double> fos_s(vm_s.size());
        for (size_t i = 0; i < vm_s.size(); ++i) {
            double f = (vm_s[i] > 1e-9) ? (yield_mpa / vm_s[i]) : 15.0;
            if (f < 0.0) f = 0.0;
            if (f > 15.0) f = 15.0;
            fos_s[i] = f;
        }

        // scale_hint: peak displacement ~10% of bbox diagonal at scale 1.
        const double bb_diag = bbox_diag(surf);
        const double peak_dm = field_max(dm_s);
        const double peak = (!dm_s.empty() && peak_dm > 1e-12) ? peak_dm : 1.0;
        const double scale_hint = (peak > 1e-12) ? (0.1 * bb_diag / peak) : 1.0;

        // vmin/vmax sourcing matches overlay.py: from summary, else field max.
        const double vmax_vm =
            summary.contains("max_von_mises_mpa") && summary["max_von_mises_mpa"].is_number()
                ? summary["max_von_mises_mpa"].get<double>()
                : field_max(vm_s);
        const double vmax_dm =
            summary.contains("max_disp_mm") && summary["max_disp_mm"].is_number()
                ? summary["max_disp_mm"].get<double>()
                : field_max(dm_s);

        std::vector<std::vector<uint8_t>> chunks;
        chunks.push_back(aicm::scalar_chunk(1, "von_mises", to_f32(vm_s), 0.0f,
                                            static_cast<float>(vmax_vm)));
        chunks.push_back(aicm::scalar_chunk(1, "displacement_mag", to_f32(dm_s), 0.0f,
                                            static_cast<float>(vmax_dm)));
        chunks.push_back(aicm::scalar_chunk(1, "fos", to_f32(fos_s), 0.0f, 5.0f));
        chunks.push_back(
            aicm::disp_chunk(1, static_cast<float>(scale_hint), to_f32(dvec_s)));

        std::string path = temp_aicm_path();
        if (!aicm::write_aicm_file(path, {bt}, chunks)) return std::string();
        return std::filesystem::absolute(path).string();
    } catch (...) {
        return std::string();
    }
}

std::string modal_overlay(const std::string& step_path,
                          const std::vector<double>& vol_points,
                          const std::vector<double>& mode_shape, double freq_hz) {
    (void)freq_hz;  // freq is metadata only (Python takes it but does not encode it).
    try {
        aicm::AicmBody bt = tessellate_step_surface(step_path);
        std::vector<double> surf = surface_verts(bt);

        std::vector<double> dvec_s = map_to_surface(surf, vol_points, mode_shape, 3);
        const size_t m = dvec_s.size() / 3;
        std::vector<double> mag(m);
        for (size_t i = 0; i < m; ++i) {
            double x = dvec_s[3 * i + 0], y = dvec_s[3 * i + 1], z = dvec_s[3 * i + 2];
            mag[i] = std::sqrt(x * x + y * y + z * z);
        }
        const double peak_mag = field_max(mag);
        const double norm = (!mag.empty() && peak_mag > 1e-12) ? peak_mag : 1.0;

        const double bb_diag = bbox_diag(surf);
        const double scale_hint = (norm > 1e-12) ? (0.15 * bb_diag / norm) : 1.0;

        std::vector<std::vector<uint8_t>> chunks;
        chunks.push_back(aicm::scalar_chunk(1, "mode_amplitude", to_f32(mag), 0.0f,
                                            static_cast<float>(norm)));
        chunks.push_back(
            aicm::disp_chunk(1, static_cast<float>(scale_hint), to_f32(dvec_s)));

        std::string path = temp_aicm_path();
        if (!aicm::write_aicm_file(path, {bt}, chunks)) return std::string();
        return std::filesystem::absolute(path).string();
    } catch (...) {
        return std::string();
    }
}

}  // namespace aicad::sim
