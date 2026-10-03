// Topology reconstruction + independent reanalysis — see topopt_reanalysis.h.
#include "sim/topopt_reanalysis.h"

#include "sim/topopt_elastic.h"
#include "sim/topopt_flow.h"
#include "sim/topopt_thermal.h"
#include "sim/topopt_thermomechanical.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <unordered_map>
#include <utility>
#include <vector>

namespace chudware {
namespace sim {
namespace topology {

using nlohmann::json;

namespace {

constexpr double kPassiveRelativeTolerance = 1e-9;
constexpr double kConformityRelativeTolerance = 1e-6;

double m3_to_mm3(double v) { return units::m3_to_mm3(v); }

// Volume (m^3) of tet k of a clipped/merged point+tet array in mm.
double tet_volume_m3(const std::vector<double> &points_mm,
                     const std::vector<int> &tets, std::size_t k) {
  const int *t = &tets[4 * k];
  auto p = [&](int i) {
    return Eigen::Vector3d(points_mm[3 * static_cast<std::size_t>(t[i])],
                           points_mm[3 * static_cast<std::size_t>(t[i]) + 1],
                           points_mm[3 * static_cast<std::size_t>(t[i]) + 2]);
  };
  const Eigen::Vector3d a = p(0);
  const double v6 = (p(1) - a).dot((p(2) - a).cross(p(3) - a));
  return units::mm3_to_m3(std::abs(v6) / 6.0);
}

struct PassiveTally {
  int elements = 0, failing = 0;
  double expected_m3 = 0, deviation_m3 = 0, present_m3 = 0;
};

json tally_solid(const PassiveTally &t) {
  return {{"elements", t.elements},
          {"volume_mm3", m3_to_mm3(t.expected_m3)},
          {"reconstructed_volume_mm3", m3_to_mm3(t.present_m3)},
          {"missing_volume_mm3", m3_to_mm3(t.deviation_m3)},
          {"incomplete_elements", t.failing}};
}

json tally_void(const PassiveTally &t) {
  return {{"elements", t.elements},
          {"volume_mm3", m3_to_mm3(t.expected_m3)},
          {"solid_volume_mm3", m3_to_mm3(t.deviation_m3)},
          {"invaded_elements", t.failing}};
}

std::uint64_t vertex_key(const SurfaceVertexSource &s) {
  if (s.node_b < 0)
    return (
        static_cast<std::uint64_t>(static_cast<std::uint32_t>(s.node_a) + 1u)
        << 32);
  const int lo = std::min(s.node_a, s.node_b),
            hi = std::max(s.node_a, s.node_b);
  return (static_cast<std::uint64_t>(static_cast<std::uint32_t>(lo) + 1u)
          << 32) |
         (static_cast<std::uint64_t>(static_cast<std::uint32_t>(hi) + 1u));
}

struct Domain {
  std::vector<double> points_mm;
  std::vector<int> tets;
  std::vector<double> density; // per tet
  json evidence = json::object();
};

// Merge the solid clip and the fluid clip into one conforming two-phase
// point/tet set on their shared node/edge-keyed vertices.
Domain merge_two_phase(const ClippedDomain &solid, const ClippedDomain &fluid) {
  Domain d;
  std::unordered_map<std::uint64_t, int> index;
  auto add = [&](const ClippedDomain &c, std::vector<int> &map) {
    const std::size_t V = c.points_mm.size() / 3;
    map.assign(V, -1);
    for (std::size_t v = 0; v < V; ++v) {
      const std::uint64_t key = vertex_key(c.vertex_source[v]);
      auto [it, inserted] =
          index.emplace(key, static_cast<int>(d.points_mm.size() / 3));
      if (inserted)
        for (int k = 0; k < 3; ++k)
          d.points_mm.push_back(c.points_mm[3 * v + k]);
      map[v] = it->second;
    }
  };
  std::vector<int> smap, fmap;
  add(solid, smap);
  const std::size_t solid_vertices = d.points_mm.size() / 3;
  add(fluid, fmap);
  const std::size_t shared =
      solid_vertices + fluid.points_mm.size() / 3 - d.points_mm.size() / 3;
  for (std::size_t k = 0; k < solid.tets.size(); ++k)
    d.tets.push_back(smap[static_cast<std::size_t>(solid.tets[k])]);
  for (std::size_t k = 0; k < solid.tets.size() / 4; ++k)
    d.density.push_back(1.0);
  for (std::size_t k = 0; k < fluid.tets.size(); ++k)
    d.tets.push_back(fmap[static_cast<std::size_t>(fluid.tets[k])]);
  for (std::size_t k = 0; k < fluid.tets.size() / 4; ++k)
    d.density.push_back(0.0);
  d.evidence = {{"solid_tets", solid.tets.size() / 4},
                {"fluid_tets", fluid.tets.size() / 4},
                {"shared_vertices", shared},
                {"vertex_keys", "mesh node or unordered mesh edge"}};
  return d;
}

double face_area_m2(const TetMesh &m, int e, int a) {
  const Eigen::Vector3d p0 = m.nodes_m.col(m.tets(kTetFaces[a][0], e));
  const Eigen::Vector3d p1 = m.nodes_m.col(m.tets(kTetFaces[a][1], e));
  const Eigen::Vector3d p2 = m.nodes_m.col(m.tets(kTetFaces[a][2], e));
  return 0.5 * (p1 - p0).cross(p2 - p0).norm();
}

double isosurface_area_m2(const SurfaceMesh &s) {
  double area = 0;
  for (int t = 0; t < s.triangle_count(); ++t) {
    if (s.triangle_origin[static_cast<std::size_t>(t)] !=
        TriangleOrigin::Isosurface)
      continue;
    const Eigen::Vector3d p0 = s.points_m.col(s.triangles(0, t));
    const Eigen::Vector3d p1 = s.points_m.col(s.triangles(1, t));
    const Eigen::Vector3d p2 = s.points_m.col(s.triangles(2, t));
    area += 0.5 * (p1 - p0).cross(p2 - p0).norm();
  }
  return area;
}

json mesh_summary(const TetMesh &m) {
  return {{"nodes", m.node_count()},
          {"elements", m.element_count()},
          {"volume_mm3", m3_to_mm3(m.total_volume_m3)},
          {"min_shape_quality", m.quality.min_shape_quality},
          {"mean_shape_quality", m.quality.mean_shape_quality},
          {"min_edge_mm", units::m_to_mm(m.quality.min_edge_m)},
          {"median_element_size_mm",
           units::m_to_mm(m.quality.median_element_size_m)},
          {"components", m.quality.components},
          {"reoriented_elements", m.quality.reoriented_elements},
          {"unreferenced_nodes", m.quality.unreferenced_nodes}};
}

ReanalysisResult failed(ReanalysisResult r, std::string code,
                        std::string message) {
  r.status = "failed";
  r.code = std::move(code);
  r.message = std::move(message);
  r.evidence["status"] = r.status;
  r.evidence["code"] = r.code;
  r.evidence["message"] = r.message;
  return r;
}

} // namespace

std::unique_ptr<Physics>
make_topology_physics(std::shared_ptr<const Problem> problem) {
  if (!problem)
    throw ModelError("invalid_input", "no prepared topology problem");
  switch (problem->settings.physics) {
  case PhysicsKind::Structural:
    return make_structural_physics(std::move(problem));
  case PhysicsKind::Modal:
  case PhysicsKind::Buckling:
    return make_spectral_physics(std::move(problem));
  case PhysicsKind::Thermal:
    return make_thermal_physics(std::move(problem));
  case PhysicsKind::Flow:
  case PhysicsKind::Cht:
    return make_flow_physics(std::move(problem));
  case PhysicsKind::Thermomechanical:
    return make_thermomechanical_physics(std::move(problem));
  }
  throw ModelError("unknown_physics", "physics has no evaluator");
}

double reconstruction_snap(double threshold) {
  return std::min(1e-4, 0.25 * std::min(threshold, 1.0 - threshold));
}

Eigen::VectorXd reconstruction_nodal_density(const Problem &p,
                                             const Eigen::VectorXd &rho,
                                             double threshold, json *evidence) {
  const TetMesh &m = *p.mesh;
  check_density(p, rho);
  Eigen::VectorXd d = nodal_average(m, rho, 1);
  int raised = 0, zeroed = 0, shared = 0, solid_nodes = 0, void_nodes = 0;
  for (int n = 0; n < m.node_count(); ++n) {
    if (!m.node_referenced[static_cast<std::size_t>(n)])
      continue;
    bool solid = false, empty = false;
    for (int k = m.node_element_offsets[n]; k < m.node_element_offsets[n + 1];
         ++k) {
      const ElementRole role =
          p.roles.role[static_cast<std::size_t>(m.node_element_indices[k])];
      solid = solid || role == ElementRole::Solid;
      empty = empty || role == ElementRole::Void;
    }
    if (solid && empty) {
      if (d[n] != threshold)
        ++shared;
      d[n] = threshold;
    } else if (solid) {
      ++solid_nodes;
      if (d[n] < threshold) {
        d[n] = threshold;
        ++raised;
      }
    } else if (empty) {
      ++void_nodes;
      if (d[n] != 0.0) {
        d[n] = 0.0;
        ++zeroed;
      }
    }
  }
  if (evidence)
    *evidence = {
        {"policy", kNodalPolicy},           {"threshold", threshold},
        {"fixed_solid_nodes", solid_nodes}, {"fixed_void_nodes", void_nodes},
        {"raised_to_threshold", raised},    {"set_to_zero", zeroed},
        {"shared_set_to_threshold", shared}};
  return d;
}

json measure_passive_geometry(const Problem &p, const ClippedDomain &clip) {
  const TetMesh &m = *p.mesh;
  const int E = m.element_count();
  std::vector<double> present(static_cast<std::size_t>(E), 0.0);
  const std::size_t T = clip.tets.size() / 4;
  for (std::size_t k = 0; k < T; ++k) {
    const int parent = clip.tet_parent[k];
    if (parent >= 0 && parent < E)
      present[static_cast<std::size_t>(parent)] +=
          tet_volume_m3(clip.points_mm, clip.tets, k);
  }
  PassiveTally solid, empty, preserve, exclude, ports, boundary;
  auto solid_add = [&](PassiveTally &t, int e) {
    const double V = m.volume_m3[e];
    const double got = present[static_cast<std::size_t>(e)];
    const double missing = std::max(0.0, V - got);
    ++t.elements;
    t.expected_m3 += V;
    t.present_m3 += got;
    t.deviation_m3 += missing;
    if (missing > kPassiveRelativeTolerance * V)
      ++t.failing;
  };
  auto void_add = [&](PassiveTally &t, int e) {
    const double V = m.volume_m3[e];
    const double got = present[static_cast<std::size_t>(e)];
    ++t.elements;
    t.expected_m3 += V;
    t.deviation_m3 += got;
    if (got > kPassiveRelativeTolerance * V)
      ++t.failing;
  };
  for (int e = 0; e < E; ++e) {
    const ElementRole role = p.roles.role[static_cast<std::size_t>(e)];
    const std::uint8_t why = p.roles.reason[static_cast<std::size_t>(e)];
    if (role == ElementRole::Solid) {
      solid_add(solid, e);
      if (why & kReasonPreserve)
        solid_add(preserve, e);
      if (why & (kReasonStructuralBoundary | kReasonThermalBoundary))
        solid_add(boundary, e);
    } else if (role == ElementRole::Void) {
      void_add(empty, e);
      if (why & kReasonExclude)
        void_add(exclude, e);
      if (why & kReasonFlowPort)
        void_add(ports, e);
    }
  }
  const bool satisfied = solid.failing == 0 && empty.failing == 0;
  return {{"measured_on", "clipped reconstructed volume (per parent element)"},
          {"relative_tolerance", kPassiveRelativeTolerance},
          {"fixed_solid", tally_solid(solid)},
          {"fixed_void", tally_void(empty)},
          {"preserve", tally_solid(preserve)},
          {"boundary_solid", tally_solid(boundary)},
          {"exclude", tally_void(exclude)},
          {"flow_ports", tally_void(ports)},
          {"void_phase", design_phase(p.settings.physics) == DesignPhase::Fluid
                             ? "fluid"
                             : "void"},
          {"satisfied", satisfied}};
}

Reconstruction reconstruct_design(const Problem &p,
                                  const Eigen::VectorXd &rho) {
  Reconstruction r;
  const TetMesh &m = *p.mesh;
  r.threshold = p.settings.density_threshold;
  r.snap = reconstruction_snap(r.threshold);
  json policy;
  r.nodal_density = reconstruction_nodal_density(p, rho, r.threshold, &policy);
  SurfaceOptions options;
  options.threshold = r.threshold;
  options.threshold_snap = r.snap;
  r.solid = clip_nodal_domain(m, r.nodal_density, options);
  json passive = measure_passive_geometry(p, r.solid.volume);
  r.passive_satisfied = passive["satisfied"].get<bool>();
  const SurfaceEvidence &se = r.solid.surface.evidence;
  r.geometry_valid = se.state == GeometryState::Valid &&
                     r.solid.volume.boundary_matches_surface &&
                     r.passive_satisfied;
  const RetainedDomain retained = retained_domain(m, rho, r.threshold);
  const DiscretizationComparison cmp =
      compare_discretizations(retained, r.solid.surface);
  json invalid = json::array();
  if (se.state != GeometryState::Valid)
    invalid.push_back(std::string("surface_") + to_string(se.state));
  if (!r.solid.volume.boundary_matches_surface)
    invalid.push_back("clipped_boundary_mismatch");
  if (!r.passive_satisfied)
    invalid.push_back("passive_regions_violated");
  r.evidence = {
      {"threshold", r.threshold},
      {"threshold_snap", r.snap},
      {"nodal_policy", policy},
      {"extraction", to_json(r.solid.surface.stats)},
      {"surface", to_json(se)},
      {"surface_state", to_string(se.state)},
      {"clipped_volume", summary_json(r.solid.volume)},
      {"reconstructed_volume_mm3", m3_to_mm3(r.solid.volume.volume_m3)},
      {"domain_volume_mm3", m3_to_mm3(m.total_volume_m3)},
      {"reconstructed_solid_fraction",
       m.total_volume_m3 > 0 ? r.solid.volume.volume_m3 / m.total_volume_m3
                             : 0.0},
      {"passive_geometry", std::move(passive)},
      {"retained_whole_elements",
       {{"role", "comparison only (raw element densities, no passive policy)"},
        {"summary", summary_json(retained)},
        {"comparison", to_json(cmp)}}},
      {"geometry_valid", r.geometry_valid},
      {"invalid_reasons", std::move(invalid)}};
  return r;
}

json reanalysis_topology(const json &topology) {
  json t = topology;
  for (const char *key :
       {"symmetry_axes", "symmetry_origin_mm", "extrusion_axis",
        "build_direction", "overhang_angle_deg", "overhang_penalty",
        "no_enclosed_voids", "min_member_mm", "preserve_thickness_mm",
        "exclude_thickness_mm", "result_dir"})
    t.erase(key);
  t["volume_fraction"] = 1.0;
  t["filter_radius_mm"] = 0.0;
  return t;
}

ReanalysisResult reanalyze_reconstruction(const Problem &design,
                                          const Reconstruction &rec,
                                          const json &topology,
                                          const BoundaryResolver &resolver,
                                          const std::atomic<bool> *cancel) {
  ReanalysisResult r;
  const PhysicsKind phys = design.settings.physics;
  const TetMesh &m = *design.mesh;
  Domain dom;
  json approximation = json::array();
  if (phys == PhysicsKind::Flow || phys == PhysicsKind::Cht) {
    // Fluid complement {d <= t}: the same policy field, mirrored.
    Eigen::VectorXd fluid_field = rec.nodal_density;
    for (Eigen::Index n = 0; n < fluid_field.size(); ++n)
      if (std::isfinite(fluid_field[n]))
        fluid_field[n] = 1.0 - fluid_field[n];
    SurfaceOptions options;
    options.threshold = 1.0 - rec.threshold;
    options.threshold_snap = rec.snap;
    const ClippedResult fluid = clip_nodal_domain(m, fluid_field, options);
    r.evidence["fluid_clip"] = summary_json(fluid.volume);
    r.evidence["fluid_surface"] = to_json(fluid.surface.evidence);
    if (phys == PhysicsKind::Flow) {
      r.domain = "clipped_fluid";
      dom.points_mm = fluid.volume.points_mm;
      dom.tets = fluid.volume.tets;
      dom.density.assign(dom.tets.size() / 4, 0.0);
      approximation.push_back(
          "pure Stokes on the reconstructed fluid channel: no Brinkman "
          "resistance; every non-port boundary of the channel is a no-slip "
          "wall");
    } else {
      r.domain = "conforming_two_phase";
      dom = merge_two_phase(rec.solid.volume, fluid.volume);
      approximation.push_back(
          "solid sub-domain modelled by Brinkman resistance alpha_max: flow "
          "through the solid is suppressed, not zero (leakage in "
          "physics_evidence)");
    }
  } else {
    r.domain = "clipped_solid";
    dom.points_mm = rec.solid.volume.points_mm;
    dom.tets = rec.solid.volume.tets;
    dom.density.assign(dom.tets.size() / 4, 1.0);
  }
  r.evidence["domain"] = r.domain;
  r.evidence["approximations"] = approximation;
  if (!dom.evidence.empty())
    r.evidence["two_phase"] = dom.evidence;
  if (dom.tets.empty())
    return failed(std::move(r), "reconstruction_empty",
                  "the reconstructed " + r.domain +
                      " domain has no volume at threshold " +
                      std::to_string(rec.threshold));

  try {
    r.mesh = build_tet_mesh(dom.points_mm, dom.tets, 4);
  } catch (const ModelError &e) {
    return failed(std::move(r), "reconstruction_mesh_invalid",
                  std::string(e.code()) + ": " + e.what());
  }
  const TetMesh &rm = *r.mesh;
  r.evidence["mesh"] = mesh_summary(rm);
  r.density = Eigen::Map<const Eigen::VectorXd>(
      dom.density.data(), static_cast<Eigen::Index>(dom.density.size()));

  if (r.domain == "conforming_two_phase") {
    double interface = 0.0;
    for (int e = 0; e < rm.element_count(); ++e)
      for (int a = 0; a < 4; ++a) {
        const int f = rm.face_neighbor(a, e);
        if (f > e && r.density[e] != r.density[f])
          interface += face_area_m2(rm, e, a);
      }
    const double iso = isosurface_area_m2(rec.solid.surface.mesh);
    const double exterior = rm.surface_area_m2.sum();
    const double domain_exterior = m.surface_area_m2.sum();
    const double vol_rel =
        std::abs(rm.total_volume_m3 - m.total_volume_m3) / m.total_volume_m3;
    const double ext_rel = std::abs(exterior - domain_exterior) /
                           std::max(domain_exterior, 1e-300);
    const double iso_rel =
        iso > 0 ? std::abs(interface - iso) / iso : std::abs(interface);
    const bool conforming = vol_rel <= kConformityRelativeTolerance &&
                            ext_rel <= kConformityRelativeTolerance &&
                            iso_rel <= kConformityRelativeTolerance;
    r.evidence["conformity"] = {
        {"interface_area_mm2", units::m2_to_mm2(interface)},
        {"solid_isosurface_area_mm2", units::m2_to_mm2(iso)},
        {"exterior_area_mm2", units::m2_to_mm2(exterior)},
        {"domain_exterior_area_mm2", units::m2_to_mm2(domain_exterior)},
        {"volume_relative_difference", vol_rel},
        {"exterior_relative_difference", ext_rel},
        {"interface_relative_difference", iso_rel},
        {"relative_tolerance", kConformityRelativeTolerance},
        {"conforming", conforming}};
    if (!conforming)
      return failed(std::move(r), "reconstruction_nonconforming",
                    "the merged solid/fluid reconstruction does not tile the "
                    "design domain conformingly (see conformity evidence)");
  }

  PreparedMesh pm;
  pm.points = dom.points_mm;
  pm.tets = dom.tets;
  pm.nodes_per_elem = 4;
  pm.referenced.assign(pm.n_nodes(), 0);
  for (int id : pm.tets)
    pm.referenced[static_cast<std::size_t>(id)] = 1;
  for (std::uint8_t used : pm.referenced)
    pm.unreferenced_nodes += used ? 0 : 1;
  pm.connected_components = rm.quality.components;

  json normalized, boundary;
  try {
    normalized = resolver(pm, reanalysis_topology(topology), &boundary);
  } catch (const ModelError &e) {
    return failed(std::move(r), "reconstruction_boundary_missing",
                  std::string(e.code()) + ": " + e.what());
  }
  r.evidence["boundary"] = boundary;

  try {
    const ProblemSpec spec = parse_problem_spec(normalized);
    r.problem = prepare_problem(r.mesh, spec);
  } catch (const ModelError &e) {
    return failed(std::move(r), "reconstruction_problem_invalid",
                  std::string(e.code()) + ": " + e.what());
  }
  r.evidence["problem"] = r.problem->evidence;

  try {
    std::unique_ptr<Physics> physics = make_topology_physics(r.problem);
    r.evidence["formulation"] = physics->formulation();
    r.evaluation = physics->evaluate(r.density, false, cancel);
  } catch (const EvaluationCancelled &) {
    throw;
  } catch (const ModelError &e) {
    return failed(std::move(r), "reconstruction_analysis_failed",
                  std::string(e.code()) + ": " + e.what());
  }
  if (cancel && cancel->load(std::memory_order_relaxed))
    throw EvaluationCancelled();
  r.evaluated = true;
  r.status = "completed";
  r.evidence["status"] = r.status;
  r.evidence["physics_evidence"] = r.evaluation.evidence;
  return r;
}

} // namespace topology
} // namespace sim
} // namespace chudware
