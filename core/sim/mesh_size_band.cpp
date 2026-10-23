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
#include <limits>

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
  // min/max constrain the automatically resolved base; they are not
  // advisor-only hints. Apply them before the governor safety check so a small
  // load/contact patch can request enough surface resolution without replacing
  // the user's fineness setting with an unrelated explicit target size.
  if (options.max_size_mm > 0.0)
    base = std::min(base, options.max_size_mm);
  if (options.min_size_mm > 0.0)
    base = std::max(base, options.min_size_mm);

  auto append_note = [&band](const char *text) {
    band.note = band.note.empty() ? std::string(text) : band.note + "; " + text;
  };

  // Memory ceiling on element count. The per-element cost is NOT an analytic
  // guess any more: governor::fea_element_limit inverts a model fitted to
  // MEASURED peak RSS of this exact solve path (tet4 15k-280k elements through
  // Eigen SparseLU, tet10 6k-149k through CHOLMOD) and carries a 1.25x safety
  // factor. The measured table, the two solver families and the safety factor
  // are documented above the calibration in core/util/resource_governor.cpp.
  // 0 means no direct-factor model: disabled/unknown governor or an iterative
  // solve admitted later against its actual pattern. Mesh ceilings remain active.
  const int nodes_per_element = options.order == 2 ? 10 : 4;
  const uint64_t memory_budget = chudware::governor::budget_bytes();
  const long memory_element_limit =
      memory_budget == UINT64_MAX || options.iterative_solver
          ? 0L
          : static_cast<long>(std::min<uint64_t>(
                chudware::governor::fea_element_limit(memory_budget, nodes_per_element, 3),
                static_cast<uint64_t>(std::numeric_limits<long>::max())));
  const char *const element_name = options.order == 2 ? "tet10" : "tet4";
  // Estimated peak for an element count, in GB, as the note reports it.
  auto estimate_gb = [nodes_per_element](double elems) {
    const uint64_t n = elems > 0.0 ? static_cast<uint64_t>(elems) : 0ull;
    return static_cast<double>(
               chudware::governor::fea_peak_bytes(n, nodes_per_element, 3)) /
           1073741824.0;
  };
  const double budget_gb =
      memory_budget == UINT64_MAX ? 0.0
                                  : static_cast<double>(memory_budget) / 1073741824.0;
  if (options.iterative_solver)
    append_note("iterative solver: direct-factor element-count limit is inapplicable; "
                "mesh allocation remains bounded and actual sparse-pattern solve "
                "admission follows meshing");

  // ── The element CEILING is a memory number, not a constant ──────────────────
  // polymesh's pre-flight guard refuses a fill past `element_ceiling`, and its
  // own default is 589,824 elements (12 x 48k, kDefaultMaxMeshElems) with a
  // 1.77M DOF twin. That constant is what a 31 GB machine hit while trying to
  // resolve four O6.6 holes: the fill it refused needed 1.16M tet4, which is
  // 0.8 GB of mesh — a rounding error against this box's study budget.
  //
  // So the ceiling is derived: study memory budget / the MEASURED mesh-stage
  // cost per element. Peak RSS of this exact path (polymesh graded fill ->
  // MeshOut normalization -> quality pass), 4-hole-plate probes 2026-09-06,
  // Release, one run per row:
  //
  //   tet4   39,952 elems -> 0.081 GB   46,304 -> 0.087   160,734 -> 0.167
  //          222,392 -> 0.201        => 706 B/element over a ~59 MB floor
  //   tet10 116,800 elems (170,917 nodes) -> 0.160 GB => 966 B/element
  //
  // The constants below carry a ~2x safety factor over those slopes and ignore
  // the fixed floor (it is noise at any element count that matters). Re-measure
  // them, never scale them, if the fill or MeshOut representation changes.
  //
  // Two things stay true regardless of how big this number gets: the automatic
  // element SIZE is still governed by the interactivity/memory budget above (a
  // raised ceiling does not make the default mesh finer), and the SOLVE is
  // gated separately by governor::fea_peak_bytes against the ACTUAL element
  // count. The ceiling's job is to stop a runaway fill, and it is floored at
  // polymesh's own default so no machine meshes less than it did before.
  constexpr uint64_t kPolymeshDefaultElementCeiling = 589824;
  constexpr uint64_t kMeshBytesPerElementTet4 = 1500;
  constexpr uint64_t kMeshBytesPerElementTet10 = 2000;
  const uint64_t mesh_bytes_per_element =
      options.order == 2 ? kMeshBytesPerElementTet10 : kMeshBytesPerElementTet4;
  band.element_ceiling =
      memory_budget == UINT64_MAX
          ? kPolymeshDefaultElementCeiling
          : std::max(kPolymeshDefaultElementCeiling,
                     memory_budget / mesh_bytes_per_element);
  band.dof_ceiling = band.element_ceiling * 3;

  const double volume_estimate =
      solid_volume > 1e-9 ? solid_volume : dx * dy * dz;
  // Bounding-box surface area — an upper bound on the model's own area, so the
  // element prediction below errs toward coarsening rather than toward OOM.
  const double area_estimate = 2.0 * (dx * dy + dy * dz + dz * dx);
  // ── Element-count prediction, MEASURED against polymesh ─────────────────────
  // The old prediction was 6 * V / h^3 (one "packing factor" times the number of
  // h-cubes that fit in the solid). On a real graded tet mesh that is wrong by more
  // than an order of magnitude, and wrong in the DANGEROUS direction. Measured on a
  // 100 mm box (V = 1e6 mm^3, bbox area = 6e4 mm^2), one polymesh run per explicit
  // target size, elements as the mesher actually produced them:
  //
  //     h (mm)   20     14     11      9      7      6      5      4     3.5
  //     elems  15264  26064  38832  52176  83184 101424 149424 148848 212592
  //     N*h^3/V  122     72     52     38     29     22     19    9.5     9.1
  //     N*h^2/A  102     85     78     70     68     61     62     40      43
  //
  // So the count does NOT scale like 1/h^3 at usable sizes: a least-squares power fit
  // gives N = 1.44e6 * h^-1.51, i.e. polymesh's graded tets are SURFACE-driven — the
  // refinement lives in a boundary layer, and the interior stays coarse. The volume
  // term only takes over at h below ~1 mm on a part this bulky.
  //
  // The model is therefore the MAX of the two dimensionally-correct terms, each with
  // a constant that upper-bounds every measured point: the surface term at 105
  // (measured max 102) and the original volume term at 6.0, which still bounds the
  // fine end (measured N*h^3/V never exceeded 122 and falls to 9 as h shrinks).
  // Re-measure both constants against polymesh, never scale them, if the mesher's
  // grading changes.
  constexpr double kTetPackingFactor = 6.0;    // elements per h^3 of volume
  constexpr double kTetSurfaceFactor = 105.0;  // elements per h^2 of surface
  // Predicted element count at target size h, and the exponent to invert it with:
  // whichever term dominates decides whether the size scales as the cube root or the
  // square root of the overshoot.
  auto predict_elements = [&](double h) {
    if (!(h > 1e-9)) return 0.0;
    const double vol_term = kTetPackingFactor * volume_estimate / (h * h * h);
    const double surf_term = kTetSurfaceFactor * area_estimate / (h * h);
    return std::max(vol_term, surf_term);
  };
  auto raise_size = [&](double h, double estimate, double budget) {
    if (!(estimate > 0.0) || !(budget > 0.0)) return h;
    const double vol_term = kTetPackingFactor * volume_estimate / (h * h * h);
    const double surf_term = kTetSurfaceFactor * area_estimate / (h * h);
    const double over = estimate / budget;
    return surf_term >= vol_term ? h * std::sqrt(over) : h * std::cbrt(over);
  };
  // ── The geometry's resolution floor ─────────────────────────────────────────
  // Coarsening for a budget is only ever worth it while the mesh it produces is
  // still a mesh. The invariant that holds, measured 2026-09-05 against this
  // exact prediction, is ELEMENTS ACROSS THE THINNEST BBOX DIRECTION — not the
  // raw side length:
  //
  //   sensor-node board  50 x 40 x 2.6 mm : 2.60 -> 4.46 mm (0.6 across) —
  //       polymesh REFUSED the fill outright ("feature unresolved at h=...:
  //       CAD face 29 has no aligned delivered boundary patch")
  //   plate with hole  120 x 60 x 3 mm    : 3.01 -> 8.80 mm (0.3 across) —
  //       32.7% poor elements, aspect_max 14.6, jacobian_min 0.075
  //   swept rod  D12 along an L-path      : 2.84 -> 5.15 mm (2.3 across) —
  //       aspect_max 339, jacobian_min 0.0023, min tet volume 0.0042 mm^3
  //       …at 3.02 mm (4.0 across): aspect_max 18.8, 26,720 elements
  //
  // The bounding box is NOT the same statement as the part's thinnest SECTION,
  // and where the two diverge this floor is inert: the hole-array bracket of
  // test_mesher_advanced (100 x 50 x 52 mm bbox over a 12 mm base plate with
  // four O8 bores plus a 12 mm upstand) has a smallest bbox side of 50 mm, so
  // the floor sat at 12.5 mm while the budget coarsened 3.93 -> 9.25 mm, and
  // the fill came back SHREDDED: 7 connected components, aspect_max 4063,
  // jacobian_min 1.9e-4, refused by fea_static. It was not even cheaper —
  // 27,690 elements at 9.25 mm and 76,562 (in 38 components) at 6.00 mm against
  // 21,764 at the honest 3.93 mm, because the fill spends its budget on feature
  // seeds trying to resolve a plate and bores it cannot fit an element into.
  // Measuring the section here instead was tried and REJECTED: the sensor-node board's
  // own thinnest section is its 1.6 mm FR4 core (its opposed 50 x 40 faces), so
  // a section-based floor of 0.4 mm is below that study's 0.5 mm base size,
  // which withdraws the coarsening 0.5 -> 0.65 mm the board depends on and
  // polymesh then refuses the fill outright ("mesh element ceiling 589824
  // exceeded"). A section thin enough to matter cannot be told from a laminate
  // layer by geometry alone — so the coarsening is judged on the RESULT instead,
  // in mesh_loaded_model / fill_with_recovery (core/sim/mesher.cpp): a size
  // polymesh refuses as unresolvable, or a fill that comes back in more
  // components than the shape has solids, falls back to `band.geometry_base`
  // (set below) — the size the geometry itself asked for.
  //
  // And the budget it defends is not even real on such parts: the honest h =
  // 3.01 mm plate mesh is 18,960 elements, 9.4x FEWER than the surface term
  // predicts, because a slab thinner than h has no two distinct boundary layers
  // and the constant fitted on a 100 mm box does not transfer there.
  //
  // So: four elements across the thinnest bbox direction is the floor, and BOTH
  // budget rungs yield to it. The clamp is inert on bulky parts (100 mm box at
  // fineness 0.25: base 8.25, raised 17.3, floor 25 — coarsening proceeds
  // untouched), and the max() below means a part ALREADY finer than the floor is
  // not coarsened at all rather than being pulled up to it.
  //
  // Why the MEMORY rung yields as well, when it is otherwise the hard one:
  //   * the automatic element size must be a function of geometry and the
  //     study's own controls, nothing else. Letting the governor push it past
  //     the floor made the sensor-node board mesh at 0.69 / 0.73 / 0.90 mm (131k,
  //     121k, 244k elements, 72 s to 525 s) across three IDENTICAL runs minutes
  //     apart, purely because free RAM moved — and two of them then failed
  //     downstream with "flux selection contains no exterior mesh face" while
  //     the third solved. sim_cache keys on the mesh OPTIONS, so a RAM-
  //     dependent size also means one cache key standing for different meshes.
  //   * nothing is left unguarded by the cap. A runaway fill is refused by
  //     polymesh's own count ceiling (kDefaultMaxMeshElems = 12 x 48k = 590k
  //     elements, kDefaultMaxMeshDof = 1.77M — the "raise --max-elems/--max-dof"
  //     in its refusals), and the SOLVE is gated separately by
  //     governor::fea_peak_bytes against the ACTUAL element count, which refuses
  //     with the binding-limit message instead of guessing. Both of those
  //     measure; this prediction only estimates, and it over-estimates by
  //     7.5-9.4x exactly where the floor binds.
  const double quality_floor =
      (std::isfinite(minimum_side) && minimum_side < 1e29) ? minimum_side / 4.0
                                                           : 0.0;
  // The size the GEOMETRY and the study's own controls asked for, before either
  // element-count budget touched it. mesh_loaded_model re-meshes here when a
  // coarsened fill comes back fragmented, so this is a load-bearing field, not
  // telemetry: it must stay the pre-coarsening value on every path.
  band.geometry_base = base;
  band.element_limit = memory_element_limit > 0
                           ? static_cast<uint64_t>(memory_element_limit)
                           : 0ull;
  band.predicted_elements = 0.0;
  if (!options.governor_probes.empty()) {
    chudware::governor::MeshCountModel measured;
    for (const auto& probe : options.governor_probes)
      measured.observe(probe.h, probe.elems);
    const double prediction = measured.predict(base);
    if (std::isfinite(prediction)) band.predicted_elements = prediction;
    append_note("measured_octave governor: preceding ladder measurements replace "
                "the bounding-box bootstrap; solve admission uses actual mesh counts");
  } else if (!explicit_size) {
    band.predicted_elements = predict_elements(base);
    long interactive_budget = static_cast<long>(8000.0 + 52000.0 * band.fineness);
    if (const char *value = std::getenv("CHUDWARE_MESH_MAX_ELEMS")) {
      const long configured = std::atol(value);
      if (configured > 0)
        interactive_budget = configured;
    }
    const bool memory_bound =
        memory_element_limit > 0 && memory_element_limit < interactive_budget;
    const long element_budget =
        memory_bound ? memory_element_limit : interactive_budget;
    if ((volume_estimate > 1e-9 || area_estimate > 1e-9) && base > 1e-9) {
      const double estimate = predict_elements(base);
      if (estimate > static_cast<double>(element_budget)) {
        const double raised =
            raise_size(base, estimate, static_cast<double>(element_budget));
        const double applied = quality_floor > 0.0
                                   ? std::max(base, std::min(raised, quality_floor))
                                   : raised;
        char text[640];
        if (applied > base * (1.0 + 1e-9)) {
          // Both branches must keep the literal "auto-coarsened" marker:
          // services/mcp/cad_sim_tools.py greps for it to detect a ladder whose
          // legs collapsed onto one budget-limited mesh (a false convergence).
          if (memory_bound)
            std::snprintf(
                text, sizeof(text),
                "auto-coarsened by the memory governor: %.0fk %s elements at %.2f mm "
                "would peak near %.1f GB, past this machine's %.1f GB study budget "
                "(calibration: %s); element size %.2f->%.2f mm (~%.0fk elements, "
                "~%.1f GB)",
                estimate / 1000.0, element_name, base, estimate_gb(estimate),
                budget_gb, chudware::governor::fea_calibration_basis(), base,
                applied, static_cast<double>(element_budget) / 1000.0,
                estimate_gb(static_cast<double>(element_budget)));
          else
            std::snprintf(text, sizeof(text),
                          "auto-coarsened for interactivity: estimated %.0fk "
                          "elements exceeds "
                          "%ldk budget; element size %.2f->%.2f mm",
                          estimate / 1000.0, element_budget / 1000, base,
                          applied);
          append_note(text);
          if (applied < raised * (1.0 - 1e-9)) {
            char capped[448];
            std::snprintf(
                capped, sizeof(capped),
                "coarsening capped at %.2f mm to keep four elements across the "
                "part's smallest dimension (%.2f mm); the %s budget alone asked "
                "for %.2f mm%s",
                applied, minimum_side,
                memory_bound ? "memory-governor" : "interactivity", raised,
                memory_bound ? " — the solve is still gated on the ACTUAL "
                               "element count, which refuses with the binding "
                               "memory limit rather than on this estimate"
                             : "");
            append_note(capped);
          }
          base = applied;
          band.memory_coarsened = memory_bound;
        } else {
          std::snprintf(
              text, sizeof(text),
              "element budget exceeded but NOT coarsened: an estimated %.0fk %s "
              "elements at %.2f mm is past the %ldk budget, but the part's "
              "smallest dimension is only %.2f mm, and coarsening below four "
              "elements across it yields a refused or unusable mesh rather than "
              "a cheaper one",
              estimate / 1000.0, element_name, base, element_budget / 1000,
              minimum_side);
          append_note(text);
        }
      }
    }
  } else {
    // An explicit size is a request for a discretization, not a bounding-box
    // solve forecast. Mesh at that size under the mesh-stage memory ceiling;
    // admission then uses the measured count. The old unmeasured bootstrap
    // consumed the entire probe-time allowance on thin/curved parts and shipped
    // its coarser mesh without ever attempting the requested resolution.
    band.predicted_elements = predict_elements(base);
    append_note("explicit size: bounding-box count is diagnostic only; "
                "mesh allocation and solve admission use their measured budgets");
  }

  // Only SAY the ceiling was raised when the raise is load-bearing: a study whose
  // own predicted count is nowhere near polymesh's default does not need the line.
  if (band.element_ceiling > kPolymeshDefaultElementCeiling &&
      predict_elements(base) > static_cast<double>(kPolymeshDefaultElementCeiling)) {
    char text[320];
    std::snprintf(text, sizeof(text),
                  "mesh element ceiling raised to %.2fM %s from polymesh's 0.59M "
                  "default: this machine's %.1f GB study budget affords it at %llu "
                  "measured mesh bytes/element",
                  static_cast<double>(band.element_ceiling) / 1e6, element_name,
                  budget_gb,
                  static_cast<unsigned long long>(mesh_bytes_per_element));
    append_note(text);
  }
  double minimum = options.min_size_mm > 0.0 ? options.min_size_mm : base * 0.5;
  double maximum = options.max_size_mm > 0.0 ? options.max_size_mm : base;
  // A LOCAL refinement request is a contract (mesher.h MeshLocalRefine): the size
  // band must not put a floor under it. min_len is what bounds the feature-hint
  // ladder and the mesher's local sizing, so an interface refined to 0.35 mm inside
  // a study whose base size is 2.9 mm needs the floor to follow it down.
  if (options.local_refine.active())
    minimum = std::min(minimum, options.local_refine.size_mm);
  if (maximum < minimum)
    std::swap(maximum, minimum);
  // The 0.1%-of-diagonal sanity floor stands, except where a local refinement
  // contract asks for less: on a 1 m part a 0.35 mm interface is legitimate.
  const double absolute_floor =
      options.local_refine.active()
          ? std::min(diagonal * 1e-3, options.local_refine.size_mm)
          : diagonal * 1e-3;
  minimum = std::max(minimum, absolute_floor);
  maximum = std::max(maximum, minimum);

  band.base = base;
  band.min_len = minimum;
  band.max_len = maximum;
  char decision[384];
  std::snprintf(decision, sizeof(decision),
                "size_band requested=%.6g base=%.6g min=%.6g max=%.6g mm; "
                "solve_element_budget=%llu mesh_element_ceiling=%llu "
                "predicted_elements=%.0f; thin_wall_floor=local "
                "feature_resolution_floor=local curvature=local",
                band.geometry_base, band.base, band.min_len, band.max_len,
                static_cast<unsigned long long>(band.element_limit),
                static_cast<unsigned long long>(band.element_ceiling),
                band.predicted_elements);
  append_note(decision);
  band.ok = true;
  return band;
}

} // namespace chudware::sim
