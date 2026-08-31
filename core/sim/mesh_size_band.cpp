#include "sim/mesh_size_band.h"

#include "util/resource_governor.h"

#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace chudware::sim {

SizeBand resolve_size_band(const TopoDS_Shape &shape,
                           const MeshOptions &options) {
  SizeBand band;
  band.fineness = std::clamp(options.fineness, 0.0, 1.0);

  double dx = 0.0;
  double dy = 0.0;
  double dz = 0.0;
  double solid_volume = 0.0;
  try {
    if (shape.IsNull()) {
      band.error = "STEP contained no shape";
      return band;
    }
    Bnd_Box bounds;
    BRepBndLib::Add(shape, bounds);
    if (bounds.IsVoid()) {
      band.error = "degenerate bounding box (void)";
      return band;
    }
    double xmin, ymin, zmin, xmax, ymax, zmax;
    bounds.Get(xmin, ymin, zmin, xmax, ymax, zmax);
    dx = std::abs(xmax - xmin);
    dy = std::abs(ymax - ymin);
    dz = std::abs(zmax - zmin);
    try {
      GProp_GProps properties;
      BRepGProp::VolumeProperties(shape, properties);
      solid_volume = std::abs(properties.Mass());
    } catch (...) {
      solid_volume = 0.0;
    }
  } catch (...) {
    band.error = "failed to read STEP bounding box";
    return band;
  }

  const double diagonal = std::sqrt(dx * dx + dy * dy + dz * dz);
  const double minimum_side = std::min(
      {dx > 1e-9 ? dx : 1e30, dy > 1e-9 ? dy : 1e30, dz > 1e-9 ? dz : 1e30});
  if (!(diagonal > 1e-6)) {
    band.error = "degenerate bounding box (diagonal <= 1e-6)";
    return band;
  }
  band.diag = diagonal;

  const bool explicit_size = options.target_size_mm > 0.0;
  const double divisor = 8.0 + 52.0 * band.fineness;
  double base = explicit_size ? options.target_size_mm : diagonal / divisor;
  if (!explicit_size && std::isfinite(minimum_side) && minimum_side < 1e29)
    base = std::min(base, minimum_side);

  auto append_note = [&band](const char *text) {
    band.note = band.note.empty() ? std::string(text) : band.note + "; " + text;
  };

  const long memory_element_limit = [&]() -> long {
    const uint64_t memory_budget = chudware::governor::budget_bytes();
    if (memory_budget == UINT64_MAX)
      return 0;
    const int nodes_per_element = options.order == 2 ? 10 : 4;
    const double bandwidth = 3.0 * nodes_per_element;
    const double bytes_per_element = bandwidth * bandwidth * 16.0 * 4.5;
    const double limit = static_cast<double>(memory_budget) / bytes_per_element;
    return limit >= 1.0 && limit < 2e18 ? static_cast<long>(limit) : 0;
  }();

  const double volume_estimate =
      solid_volume > 1e-9 ? solid_volume : dx * dy * dz;
  constexpr double kTetPackingFactor = 6.0;
  if (!explicit_size) {
    long element_budget = static_cast<long>(8000.0 + 52000.0 * band.fineness);
    if (const char *value = std::getenv("CHUDWARE_MESH_MAX_ELEMS")) {
      const long configured = std::atol(value);
      if (configured > 0)
        element_budget = configured;
    }
    if (memory_element_limit > 0 && memory_element_limit < element_budget)
      element_budget = memory_element_limit;
    if (volume_estimate > 1e-9 && base > 1e-9) {
      const double estimate =
          kTetPackingFactor * volume_estimate / (base * base * base);
      if (estimate > static_cast<double>(element_budget)) {
        const double raised =
            base * std::cbrt(estimate / static_cast<double>(element_budget));
        char text[224];
        std::snprintf(text, sizeof(text),
                      "auto-coarsened for interactivity: estimated %.0fk "
                      "elements exceeds "
                      "%ldk budget; element size %.2f->%.2f mm",
                      estimate / 1000.0, element_budget / 1000, base, raised);
        append_note(text);
        base = raised;
      }
    }
  } else if (memory_element_limit > 0 && volume_estimate > 1e-9 &&
             base > 1e-9) {
    const double estimate =
        kTetPackingFactor * volume_estimate / (base * base * base);
    if (estimate > static_cast<double>(memory_element_limit)) {
      const double raised =
          base *
          std::cbrt(estimate / static_cast<double>(memory_element_limit));
      char text[240];
      std::snprintf(
          text, sizeof(text),
          "governor: target size %.2f mm needs about %.0fk elements, beyond "
          "this machine's %.0fk-element memory budget; raised to %.2f mm",
          base, estimate / 1000.0,
          static_cast<double>(memory_element_limit) / 1000.0, raised);
      append_note(text);
      base = raised;
    }
  }

  double minimum = options.min_size_mm > 0.0 ? options.min_size_mm : base * 0.5;
  double maximum = options.max_size_mm > 0.0 ? options.max_size_mm : base;
  if (maximum < minimum)
    std::swap(maximum, minimum);
  minimum = std::max(minimum, diagonal * 1e-3);
  maximum = std::max(maximum, minimum);

  band.base = base;
  band.min_len = minimum;
  band.max_len = maximum;
  band.ok = true;
  return band;
}

} // namespace chudware::sim
