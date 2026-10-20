// Topology optimization: common typed problem model (OCCT-free, Eigen + JSON).
//
// One prepared, immutable `Problem` serves all seven physics (structural,
// thermal, modal, buckling, flow, thermomechanical, CHT); there is no second
// problem schema. Physics evaluators (factories in their own modules) implement
// `Physics` and read only the typed members below; the optimizer composes
// `DesignMap` + `DensityFilter` + `DesignMap::project_ties`.
//
// Units. Inputs arrive in user units (mm, N, MPa, W, C, W/mK, W/m2K, m/s, Pa,
// Pa*s, g/cm3). `prepare_problem` converts them ONCE to SI base units, and
// every member of TetMesh / Problem / Evaluation is SI: m, m^2, m^3, kg/m^3, N,
// Pa, W, K (absolute), W/(m K), W/(m^2 K), J/(kg K), 1/K, m/s, Pa*s.
// Report-unit conversion back is the adapter's job (see `units`).
//
// Density convention. Element density rho_e in [0,1] is the SOLID fraction
// everywhere: rho = 1 is solid, rho = 0 is void (structural/thermal) or fluid
// (flow/CHT). Solid-phase properties interpolate as P(rho) = P_solid * (eps +
// (1 - eps) f(rho)), eps = settings.min_density (a property floor ratio, not a
// variable bound), f from `Interpolation`.
//
// Normalized adapter JSON (`parse_problem_spec`); every node index is a 0-based
// tet4 corner-node index into the mesh passed to `prepare_problem`; unknown
// keys refuse:
// {
//   "topology": { physics, method, objective?, volume_fraction,
//   max_iterations?,
//     change_tolerance?, filter_radius_mm?, projection_beta?, penalty?,
//     ramp_q?, min_density?, move_limit?, density_threshold?,
//     stress_limit_mpa?, max_displacement_mm?, max_temperature_c?,
//     min_frequency_hz?, min_buckling_factor?, max_pressure_drop_pa?,
//     symmetry_axes?:["x"|"y"|"z"], symmetry_origin_mm?:[3],
//     extrusion_axis?:"x"|"y"|"z", min_member_mm?, build_direction?:[3],
//     overhang_angle_deg?, overhang_penalty?, no_enclosed_voids?,
//     preserve_thickness_mm?, exclude_thickness_mm?, beso_evolution_rate?,
//     level_set_regularization?, result_dir? },
//   "material": { E_mpa?, nu?, yield_mpa?, density_g_cm3?, k_w_mk?,
//   alpha_per_k?,
//     cp_j_kgk?, name? },
//   "fixtures":   [ {label?, nodes:[int], dofs?:[0,1,2]} ],            //
//   shared structural "load_cases": [ {name?, weight?, fixtures?:[...],
//   forces_n:[3*N]} ], // nodal N, resolved "thermal": {
//   reference_temperature_c?, fixtures:[{label?, nodes, temperature_c}],
//     loads:[ {label?, nodes, type:"flux", magnitude_w_m2}
//           | {label?, nodes, type:"power", power_w}
//           | {label?, nodes, type:"convection", h_w_m2k, ambient_c} ] },
//   "flow": { viscosity_pa_s, density_kg_m3, brinkman_max?,
//   reference_length_mm?,
//     fluid_k_w_mk?, fluid_cp_j_kgk?,
//     inlets:[{label?, nodes, velocity_m_s:[3], temperature_c?}],
//     outlets:[{label?, nodes, pressure_pa}], walls?:[{label?, nodes}] },
//   "preserve": [ {label?, nodes:[int], thickness_mm?} | {label?,
//   box_min_mm:[3], box_max_mm:[3]} ], "exclude":  [ same as preserve ],
//   "build_plate": {label?, nodes:[int]} // resolved design-domain bed face
// }
// Thermal/flow node sets select exterior triangles whose three corners are all
// in the set.
#pragma once

#include <Eigen/Dense>
#include <Eigen/Sparse>
#include <nlohmann/json.hpp>

#include <array>
#include <atomic>
#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace chudware {
namespace sim {
namespace topology {

// ── Errors
// ──────────────────────────────────────────────────────────────────────────
// Every refusal carries a stable machine code (e.g. "degenerate_element",
// "passive_conflict", "rigid_body_motion") plus a human message naming the
// input.
class ModelError : public std::runtime_error {
public:
  ModelError(std::string code, const std::string &message);
  const std::string &code() const { return code_; }

private:
  std::string code_;
};

// Thrown by Physics::evaluate when its cancel flag is raised.
class EvaluationCancelled : public std::runtime_error {
public:
  EvaluationCancelled() : std::runtime_error("topology evaluation cancelled") {}
};

// ── Units
// ───────────────────────────────────────────────────────────────────────────
namespace units {
inline constexpr double kMeterPerMm = 1e-3;
inline constexpr double kPaPerMpa = 1e6;
inline constexpr double kKgM3PerGCm3 = 1e3;
inline constexpr double kKelvinAtZeroCelsius = 273.15;
inline double mm_to_m(double v) { return v * kMeterPerMm; }
inline double m_to_mm(double v) { return v / kMeterPerMm; }
inline double mm2_to_m2(double v) { return v * kMeterPerMm * kMeterPerMm; }
inline double m2_to_mm2(double v) { return v / (kMeterPerMm * kMeterPerMm); }
inline double mm3_to_m3(double v) {
  return v * kMeterPerMm * kMeterPerMm * kMeterPerMm;
}
inline double m3_to_mm3(double v) {
  return v / (kMeterPerMm * kMeterPerMm * kMeterPerMm);
}
inline double mpa_to_pa(double v) { return v * kPaPerMpa; }
inline double pa_to_mpa(double v) { return v / kPaPerMpa; }
inline double g_cm3_to_kg_m3(double v) { return v * kKgM3PerGCm3; }
inline double kg_m3_to_g_cm3(double v) { return v / kKgM3PerGCm3; }
inline double celsius_to_kelvin(double v) { return v + kKelvinAtZeroCelsius; }
inline double kelvin_to_celsius(double v) { return v - kKelvinAtZeroCelsius; }
} // namespace units

// ── Vocabulary
// ──────────────────────────────────────────────────────────────────────
enum class PhysicsKind {
  Structural,
  Thermal,
  Modal,
  Buckling,
  Flow,
  Thermomechanical,
  Cht
};
enum class Method { Simp, Ramp, Beso, LevelSet };
enum class ObjectiveKind {
  Compliance,
  Mass,
  ThermalCompliance,
  Frequency,
  BucklingFactor,
  Dissipation,
  MeanTemperature
};
enum class ObjectiveSense { Minimize, Maximize };
// Phase whose volume `volume_fraction` budgets: solid for solid physics, fluid
// for flow/CHT (channels are the optimized material there).
enum class DesignPhase { Solid, Fluid };

const char *to_string(PhysicsKind);
const char *to_string(Method);
const char *to_string(ObjectiveKind);
const char *to_string(DesignPhase);             // "solid" | "fluid"
PhysicsKind parse_physics(const std::string &); // ModelError "unknown_physics"
Method parse_method(const std::string &);       // ModelError "unknown_method"
ObjectiveKind
parse_objective(const std::string &); // ModelError "unknown_objective"

ObjectiveSense objective_sense(ObjectiveKind);
DesignPhase design_phase(PhysicsKind);
// Contract objective table; first entry is the default for the physics.
std::vector<ObjectiveKind> objectives_for(PhysicsKind);
bool uses_structural_boundary(
    PhysicsKind); // structural, modal, buckling, thermomechanical
bool uses_thermal_boundary(PhysicsKind); // thermal, thermomechanical, cht
bool uses_flow_boundary(PhysicsKind);    // flow, cht

// ── Material interpolation
// ────────────────────────────────────────────────────────── Simp: f = rho^p.
// Ramp: f = rho / (1 + q (1 - rho)).  f(0)=0, f(1)=1.
struct Interpolation {
  enum class Kind { Simp, Ramp } kind = Kind::Simp;
  double exponent = 3.0; // p (Simp, >= 1) or q (Ramp, >= 0)
  double floor = 1e-4;   // eps in P = P_solid (eps + (1 - eps) f)

  double shape(double rho) const;            // f(rho)
  double shape_derivative(double rho) const; // f'(rho)
  double scale(double rho) const;            // eps + (1 - eps) f
  double scale_derivative(double rho) const; // (1 - eps) f'
};
// simp -> Simp(penalty); ramp -> Ramp(ramp_q); beso -> Simp(penalty) soft-kill;
// level_set -> Simp(1): linear ersatz material over the regularized Heaviside
// density.
struct Settings;
Interpolation interpolation_for(const Settings &);

// ── Settings (the public `topology` object, validated)
// ──────────────────────────────
struct Settings {
  PhysicsKind physics = PhysicsKind::Structural;
  Method method = Method::Simp;
  ObjectiveKind objective = ObjectiveKind::Compliance;
  double volume_fraction = 0.0;   // (0,1], required
  int max_iterations = 200;       // >= 1
  double change_tolerance = 0.01; // > 0
  std::optional<double>
      filter_radius_mm;           // >= 0; absent -> min_member/2 or 2*median h
  double projection_beta = 0.0;   // >= 0 (simp/ramp only)
  double penalty = 3.0;           // >= 1 (simp/beso only)
  double ramp_q = 5.0;            // >= 0 (ramp only)
  double min_density = 1e-4;      // (0,1): property floor ratio eps
  double move_limit = 0.2;        // (0,1] (simp/ramp only)
  double density_threshold = 0.5; // (0,1)
  std::optional<double> stress_limit_mpa;     // structural, thermomechanical
  std::optional<double> max_displacement_mm;  // structural, thermomechanical
  std::optional<double> max_temperature_c;    // thermal, thermomechanical, cht
  std::optional<double> min_frequency_hz;     // modal
  std::optional<double> min_buckling_factor;  // buckling
  std::optional<double> max_pressure_drop_pa; // flow, cht
  std::array<bool, 3> symmetry_axes{false, false,
                                    false}; // mirror plane normal to axis
  std::optional<Eigen::Vector3d>
      symmetry_origin_mm;              // absent -> mesh bbox center
  int extrusion_axis = -1;             // -1 none, else 0..2
  std::optional<double> min_member_mm; // intent only, not a guarantee
  std::optional<Eigen::Vector3d>
      build_direction;              // unit-normalized when present
  double overhang_angle_deg = 45.0; // (0,90)
  double overhang_penalty = 0.0;    // >= 0
  bool no_enclosed_voids = false;
  double preserve_thickness_mm =
      0.0; // >= 0, depth of preserve regions without thickness
           // and of the automatic BC solid layer
  double exclude_thickness_mm =
      0.0; // >= 0, depth of exclude regions without thickness
  double beso_evolution_rate = 0.02;      // (0,1) (beso only)
  double level_set_regularization = 5e-4; // >= 0 (level_set only)
  std::string result_dir;                 // validated at service boundary
};
// Validates types, ranges, method/physics/objective/limit applicability;
// refuses unknown keys. Does not decide capability support (that is
// topology_capabilities()).
Settings parse_settings(const nlohmann::json &topology,
                        bool has_build_plate = false);

// ── Material
// ────────────────────────────────────────────────────────────────────────
struct MaterialInput { // user units
  std::optional<double> e_mpa, nu, yield_mpa, density_g_cm3, k_w_mk,
      alpha_per_k, cp_j_kgk;
  std::string name;
};
MaterialInput parse_material(const nlohmann::json &material);

struct Material { // SI; prepare_problem guarantees presence of what the physics
                  // needs
  std::optional<double> youngs_pa, poisson, yield_pa, density_kg_m3,
      conductivity_w_mk, expansion_per_k, specific_heat_j_kgk;
  std::string name;
};
// Range-checks present values; ModelError "invalid_material".
Material convert_material(const MaterialInput &);
// ModelError "material_missing" naming the absent key (E_mpa, nu,
// density_g_cm3, k_w_mk, alpha_per_k, cp_j_kgk) required by `physics`.
void require_material(const Material &, PhysicsKind physics);

// Voigt order [xx, yy, zz, xy, yz, zx] with engineering shear strains.
Eigen::Matrix<double, 6, 6> isotropic_elasticity(double youngs_pa,
                                                 double poisson);

// ── Mesh
// ────────────────────────────────────────────────────────────────────────────
struct MeshQuality {
  double min_volume_m3 = 0, max_volume_m3 = 0;
  double min_shape_quality = 0,
         mean_shape_quality = 0; // 6*sqrt(2)*V / l_rms^3, 1 = regular
  double min_edge_m = 0, max_edge_m = 0;
  double median_element_size_m = 0; // median of element_size_m
  int reoriented_elements = 0;      // negative-orientation input tets flipped
  int unreferenced_nodes = 0;
  int components = 0; // face-connected element components
};

// Local face a of a tet is opposite local node a; kTetFaces[a] lists the other
// three local nodes so the right-hand normal points out of the (positively
// oriented) tet.
inline constexpr int kTetFaces[4][3] = {
    {1, 2, 3}, {0, 3, 2}, {0, 1, 3}, {0, 2, 1}};

struct TetMesh {
  Eigen::Matrix3Xd nodes_m; // 3 x N
  Eigen::Matrix4Xi tets;    // 4 x E, positive orientation after preparation
  Eigen::Matrix3Xd grad_n;  // 3 x 4E: column 4e+a = grad N_a of element e (1/m)
  Eigen::VectorXd volume_m3;   // E, > 0
  Eigen::Matrix3Xd centroid_m; // 3 x E
  Eigen::VectorXd
      element_size_m; // E, cbrt(6*sqrt(2)*V): edge of equal-volume regular tet
  Eigen::Matrix4Xi
      face_neighbor; // 4 x E, element across local face a, -1 = exterior
  Eigen::VectorXi node_element_offsets; // N+1 CSR over incident elements
  Eigen::VectorXi node_element_indices;
  Eigen::Matrix3Xi
      surface_nodes; // 3 x F exterior triangles, outward right-hand order
  Eigen::VectorXi surface_element;    // F owning element
  Eigen::VectorXi surface_local_face; // F local face (opposite local node)
  Eigen::VectorXd surface_area_m2;    // F
  Eigen::Matrix3Xd surface_normal;    // 3 x F unit outward
  std::vector<std::uint8_t>
      node_referenced; // N, 0 = no element uses the node (must pin)
  std::vector<std::uint8_t>
      node_exterior;                   // N, 1 = corner of an exterior triangle
  std::vector<int> unreferenced_nodes; // sorted
  Eigen::Vector3d bbox_min_m = Eigen::Vector3d::Zero(),
                  bbox_max_m = Eigen::Vector3d::Zero();
  double total_volume_m3 = 0;
  MeshQuality quality;

  int node_count() const { return static_cast<int>(nodes_m.cols()); }
  int element_count() const { return static_cast<int>(tets.cols()); }
  int surface_count() const { return static_cast<int>(surface_nodes.cols()); }
  auto element_gradients(int e) const { return grad_n.middleCols<4>(4 * e); }
};

// points_mm flat xyz, connectivity flat (nodes_per_elem per element). Refuses
// (ModelError): unsupported_element_order, invalid_mesh (sizes, non-finite,
// index range, repeated node, empty), duplicate_element, degenerate_element,
// nonmanifold_face. Negative orientation is repaired by swapping local nodes 2
// and 3 and counted.
std::shared_ptr<const TetMesh>
build_tet_mesh(const std::vector<double> &points_mm,
               const std::vector<int> &connectivity, int nodes_per_elem = 4);

// Exterior triangles whose three corners are all in `nodes` (sorted or not).
std::vector<int> surface_faces_with_nodes(const TetMesh &,
                                          const std::vector<int> &nodes);
// Elements incident to any of `nodes`.
std::vector<int> elements_touching_nodes(const TetMesh &,
                                         const std::vector<int> &nodes);

// Tet4 small-strain matrix (6 x 12, DOF order [u0x,u0y,u0z,u1x,...]) from 3 x 4
// gradients.
Eigen::Matrix<double, 6, 12>
tet4_strain_matrix(const Eigen::Ref<const Eigen::Matrix<double, 3, 4>> &grad);

// Element DOF map, (4*components) x E: entry (components*a + c, e) =
// base_offset + components*tets(a,e) + c (node-major). Stack maps for mixed
// fields.
Eigen::MatrixXi nodal_dof_map(const TetMesh &, int components,
                              int base_offset = 0);

// ── Dirichlet / DOF reduction
// ───────────────────────────────────────────────────────
struct Dirichlet {
  Eigen::VectorXi dofs;   // sorted unique full-numbering DOFs
  Eigen::VectorXd values; // prescribed values (SI), same length
  int size() const { return static_cast<int>(dofs.size()); }
};
// Merges duplicates; equal values (|a-b| <= 1e-12*max(1,|a|,|b|)) merge, others
// refuse "dirichlet_conflict". Refuses out-of-range dofs and non-finite values.
Dirichlet make_dirichlet(int full_size,
                         const std::vector<std::pair<int, double>> &entries);

struct DofReduction {
  int full_size = 0;
  Eigen::VectorXi free_to_full; // n_free
  Eigen::VectorXi full_to_free; // full_size, -1 = constrained
  Eigen::VectorXd
      prescribed; // full_size, Dirichlet value on constrained, 0 on free

  int free_size() const { return static_cast<int>(free_to_full.size()); }
  void restrict_vector(const Eigen::VectorXd &full,
                       Eigen::VectorXd &free) const;
  // full = free values on free DOFs, prescribed values on constrained DOFs.
  void expand_vector(const Eigen::VectorXd &free, Eigen::VectorXd &full) const;
};
DofReduction make_dof_reduction(int full_size, const Dirichlet &);

// Fixed CSC pattern of the reduced (free x free) operator for an element DOF
// map, with a precomputed element-entry -> value slot table so iterations
// re-assemble values in place (pattern stays identical: analyzePattern once,
// factorize per iteration). Full (both triangles) pattern; valid for symmetric
// and unsymmetric operators. Memory: E * k^2 ints for the slot table.
class ReducedPattern {
public:
  ReducedPattern() = default;
  ReducedPattern(const Eigen::MatrixXi &element_dofs,
                 const DofReduction &reduction);

  const Eigen::SparseMatrix<double> &pattern() const { return pattern_; }
  int dofs_per_element() const {
    return static_cast<int>(element_dofs_.rows());
  }
  int element_count() const { return static_cast<int>(element_dofs_.cols()); }
  const DofReduction &reduction() const { return reduction_; }

  // Zero K's values (K must carry pattern()'s structure).
  static void zero(Eigen::SparseMatrix<double> &K);
  // K_ff += ke restricted to free rows/cols of element e.
  void add(Eigen::SparseMatrix<double> &K, int e,
           const Eigen::Ref<const Eigen::MatrixXd> &ke) const;
  // rhs_free -= ke(free rows, constrained cols) * prescribed(constrained cols).
  void lift(Eigen::VectorXd &rhs_free, int e,
            const Eigen::Ref<const Eigen::MatrixXd> &ke) const;

private:
  Eigen::MatrixXi element_dofs_;
  DofReduction reduction_;
  Eigen::SparseMatrix<double> pattern_;
  std::vector<int> slot_; // E*k*k, -1 when row or col constrained
};

// ── Resolved boundary representation (SI)
// ───────────────────────────────────────────
struct NodeSet {
  std::string label;
  std::vector<int> nodes; // sorted unique
};
struct SurfacePatch {
  std::string label;
  std::vector<int> nodes; // sorted unique, as resolved
  std::vector<int>
      faces; // indices into TetMesh surface arrays (all-corner rule)
  double area_m2 = 0;
};

struct BuildPlate {
  int triangles = 0;
  double area_m2 = 0;
  Eigen::Vector3d normal = Eigen::Vector3d::Zero(); // outward
};

struct StructuralFixture {
  std::string label;
  std::vector<int> nodes;
  std::array<bool, 3> dofs{true, true, true};
};
struct StructuralCase {
  std::string name;
  double weight = 1.0;     // > 0; objective = sum weight_i * response_i
  Eigen::VectorXd force_n; // 3N nodal forces
  Eigen::Vector3d resultant_n = Eigen::Vector3d::Zero();
  std::vector<StructuralFixture>
      fixtures;    // case-specific (shared ones in boundary)
  Dirichlet fixed; // dof 3n+c, value 0 m; shared+case+unreferenced pins
  std::vector<int> load_nodes; // nodes with nonzero force
};
struct StructuralBoundary {
  std::vector<StructuralFixture> fixtures; // shared
  Dirichlet shared_fixed; // shared fixtures + unreferenced pins (modal)
  std::vector<StructuralCase> cases; // empty for modal
  bool cases_share_constraints =
      true; // all case.fixed identical -> one reduction
};

struct ThermalFixture {
  std::string label;
  std::vector<int> nodes;
  double temperature_k = 0;
};
enum class ThermalLoadKind { Flux, Power, Convection };
struct ThermalFlux {
  SurfacePatch patch;
  ThermalLoadKind source = ThermalLoadKind::Flux; // Flux or Power (power/area)
  double flux_w_m2 = 0;                           // inward heat flux density
  double input_value = 0;                         // as given: W/m2 or W
};
struct ThermalConvection {
  SurfacePatch patch;
  double h_w_m2k = 0;
  double ambient_k = 0;
};
struct ThermalBoundary {
  double reference_temperature_k = units::celsius_to_kelvin(20.0);
  bool reference_given = false;
  std::vector<ThermalFixture> fixtures;
  Dirichlet fixed; // dof = node, kelvin; fixtures, CHT inlet temps,
                   // unreferenced nodes pinned at reference temperature
  std::vector<ThermalFlux> fluxes;
  std::vector<ThermalConvection> convections;
  double total_heat_input_w = 0; // sum flux * area
};

struct FlowInlet {
  SurfacePatch patch;
  Eigen::Vector3d velocity_m_s = Eigen::Vector3d::Zero();
  std::optional<double> temperature_k;
  double nominal_flow_m3_s = 0; // -sum (v . n) A over the patch, > 0 (inflow)
};
struct FlowOutlet {
  SurfacePatch patch;
  double pressure_pa = 0; // traction (do-nothing) pressure, natural BC
};
struct FlowBoundary {
  double viscosity_pa_s = 0, density_kg_m3 = 0;
  std::optional<double> fluid_conductivity_w_mk, fluid_specific_heat_j_kgk;
  double brinkman_max = 1e5; // dimensionless inverse Darcy number as given
  double reference_length_m =
      0; // given, else equivalent inlet diameter sqrt(4A/pi)
  double alpha_max =
      0; // Brinkman coefficient at rho=1: brinkman_max*mu/L^2 (Pa s/m^2)
  std::vector<FlowInlet> inlets;
  std::vector<FlowOutlet> outlets;
  std::vector<SurfacePatch>
      walls;                   // explicit labels (subset of no-slip surface)
  std::vector<int> wall_faces; // every exterior triangle not in a port: no-slip
  Dirichlet velocity; // dof 3n+c: wall/rim/unreferenced 0, inlet interior v
  int rim_nodes_no_slip = 0; // port nodes shared with walls, assigned no-slip
  double mean_inlet_speed_m_s = 0; // total nominal Q / total inlet area
  double reynolds = 0; // rho U L / mu (regime evidence, not a validity claim)
};

// ── Passive regions and ownership
// ───────────────────────────────────────────────────
enum class ElementRole : std::uint8_t { Design = 0, Solid = 1, Void = 2 };
enum PassiveReason : std::uint8_t {
  kReasonPreserve = 1,
  kReasonExclude = 2,
  kReasonStructuralBoundary = 4,
  kReasonThermalBoundary = 8,
  kReasonFlowPort = 16,
  kReasonTied = 32
};
struct ElementRoles {
  std::vector<ElementRole> role;    // E
  std::vector<std::uint8_t> reason; // E, PassiveReason bits
  int design_count = 0, solid_count = 0, void_count = 0;
  std::vector<std::uint8_t> solid_mask() const;
  std::vector<std::uint8_t> void_mask() const;
  void recount();
};

enum NodeOwnershipBit : std::uint16_t {
  kNodeStructuralFixed = 1,
  kNodeStructuralLoad = 2,
  kNodeThermalFixed = 4,
  kNodeThermalFlux = 8,
  kNodeThermalConvection = 16,
  kNodeFlowInlet = 32,
  kNodeFlowOutlet = 64,
  kNodeFlowWall = 128,
  kNodeUnreferenced = 256,
  kNodeExterior = 512
};

// Element region in user units, mesh-resolved. Box: centroid inside [min,max].
// Node set: elements touching the nodes, plus (thickness > 0) elements whose
// centroid lies within thickness of the selected exterior triangles (or of the
// nodes when no triangle has all corners selected).
struct RegionSpec {
  std::string label;
  std::vector<int> nodes;
  std::optional<double> thickness_mm;
  bool has_box = false;
  Eigen::Vector3d box_min_mm = Eigen::Vector3d::Zero(),
                  box_max_mm = Eigen::Vector3d::Zero();
};
std::vector<int> region_elements(const TetMesh &, const RegionSpec &,
                                 double default_thickness_m);

// ── Design variables: tying and filtering
// ───────────────────────────────────────────
struct TyingSpec {
  std::array<bool, 3> mirror{false, false, false};
  Eigen::Vector3d mirror_origin_m = Eigen::Vector3d::Zero();
  int extrusion_axis = -1;     // -1 none
  double extrusion_cell_m = 0; // in-plane bin size; 0 -> median element size
};

// rho = expand(x): rho_e = x_{v(e)} for design elements, fixed (1 solid / 0
// void) otherwise. reduce_gradient is the exact transpose of the linear part
// (an unweighted SUM over each variable's elements).
//
// project_ties restores the ties after any element-wise linear operator (the
// density filter) has mixed neighbours: each group becomes its volume-weighted
// mean  T y |_e = sum_{f in v(e)} V_f y_f / V_v,  V_v = variable_volume_m3.
// That normalization is the tie's own (sum of tie_weight over a group is 1)
// and is independent of reduce_gradient's unweighted sum. T preserves
// sum_e V_e y_e exactly, is idempotent, keeps [0,1] bounds, and leaves
// passive and single-element entries untouched. project_ties_transpose applies
// T^T: g_e = tie_weight_e * sum_{f in v(e)} g_f. Both work in place in O(E).
// An element-wise scalar map applied AFTER project_ties (e.g. a Heaviside
// projection) keeps the ties, and its derivative is constant within a group,
// so in reverse it commutes with project_ties_transpose.
struct DesignMap {
  int variable_count = 0;
  Eigen::VectorXi element_variable; // E, -1 = passive
  Eigen::VectorXd fixed_density; // E, 1 solid / 0 void on passive, 0 on design
  Eigen::VectorXi variable_offsets; // V+1 CSR
  Eigen::VectorXi variable_elements;
  Eigen::VectorXd variable_volume_m3; // V
  Eigen::VectorXd tie_weight;    // E, V_e / variable_volume_m3[v(e)]; 0 passive
  double max_tie_distance_m = 0; // largest mirror-image mismatch accepted
  int tied_groups = 0;           // variables with >1 element

  void expand(const Eigen::VectorXd &x, Eigen::VectorXd &rho) const;
  void reduce_gradient(const Eigen::VectorXd &g_rho,
                       Eigen::VectorXd &g_x) const;
  // ModelError "invalid_density" on a size mismatch.
  void project_ties(Eigen::VectorXd &y) const;
  void project_ties_transpose(Eigen::VectorXd &g) const;
};
// Ties mirror images (nearest centroid; refuses "symmetry_domain_mismatch"
// beyond the local element size) and extrusion columns (in-plane bins). Groups
// touching passive elements become passive (reason kReasonTied); groups with
// both solid and void refuse "tie_passive_conflict". `roles` is updated in
// place.
DesignMap build_design_map(const TetMesh &, ElementRoles &roles,
                           const TyingSpec &);

// Volume-weighted centroid hat filter: rho~_e = sum_f w_ef V_f rho_f / sum_f
// w_ef V_f, w_ef = max(0, R - |c_e - c_f|). Rows sum to 1 (constants
// preserved). R = 0 -> identity.
struct DensityFilter {
  double radius_m = 0;
  Eigen::SparseMatrix<double, Eigen::RowMajor> weights; // E x E normalized
  void apply(const Eigen::VectorXd &rho, Eigen::VectorXd &out) const;
  void apply_transpose(const Eigen::VectorXd &g, Eigen::VectorXd &out) const;
};
DensityFilter build_density_filter(const TetMesh &, double radius_m);

// ── Prepared problem
// ────────────────────────────────────────────────────────────────
struct FixtureSpec {
  std::string label;
  std::vector<int> nodes;
  std::array<bool, 3> dofs{true, true, true};
};
struct LoadCaseSpec {
  std::string name;
  double weight = 1.0;
  std::vector<FixtureSpec> fixtures;
  std::vector<double> forces_n; // 3N
};
struct ThermalFixtureSpec {
  std::string label;
  std::vector<int> nodes;
  double temperature_c = 0;
};
struct ThermalLoadSpec {
  std::string label;
  std::vector<int> nodes;
  ThermalLoadKind kind = ThermalLoadKind::Flux;
  double magnitude_w_m2 = 0, power_w = 0, h_w_m2k = 0, ambient_c = 0;
};
struct ThermalSpec {
  std::optional<double> reference_temperature_c;
  std::vector<ThermalFixtureSpec> fixtures;
  std::vector<ThermalLoadSpec> loads;
};
struct FlowInletSpec {
  std::string label;
  std::vector<int> nodes;
  Eigen::Vector3d velocity_m_s = Eigen::Vector3d::Zero();
  std::optional<double> temperature_c;
};
struct FlowOutletSpec {
  std::string label;
  std::vector<int> nodes;
  double pressure_pa = 0;
};
struct FlowSpec {
  double viscosity_pa_s = 0, density_kg_m3 = 0;
  double brinkman_max = 1e5;
  std::optional<double> reference_length_mm, fluid_k_w_mk, fluid_cp_j_kgk;
  std::vector<FlowInletSpec> inlets;
  std::vector<FlowOutletSpec> outlets;
  std::vector<NodeSet> walls;
};
struct ProblemSpec {
  Settings settings;
  MaterialInput material;
  std::vector<FixtureSpec> fixtures; // shared structural
  std::vector<LoadCaseSpec> load_cases;
  std::optional<ThermalSpec> thermal;
  std::optional<FlowSpec> flow;
  std::optional<NodeSet> build_plate;
  std::vector<RegionSpec> preserve, exclude;
};
ProblemSpec parse_problem_spec(const nlohmann::json &normalized);

struct ResponseLimits { // SI
  std::optional<double> stress_pa, displacement_m, temperature_k, frequency_hz,
      buckling_factor, pressure_drop_pa;
};

struct Problem {
  std::shared_ptr<const TetMesh> mesh;
  Settings settings;
  Material material;
  Interpolation interpolation;
  ResponseLimits limits;
  double filter_radius_m = 0;
  std::string filter_radius_source; // explicit | min_member_mm |
                                    // auto_2x_median_element_size
  std::optional<double> min_member_m;
  std::optional<BuildPlate> build_plate;
  double preserve_thickness_m = 0, exclude_thickness_m = 0;
  std::optional<StructuralBoundary> structural; // iff uses_structural_boundary
  std::optional<ThermalBoundary> thermal;       // iff uses_thermal_boundary
  std::optional<FlowBoundary> flow;             // iff uses_flow_boundary
  ElementRoles roles;
  std::vector<std::uint16_t> node_ownership; // N, NodeOwnershipBit
  DesignMap design;
  DensityFilter filter;
  nlohmann::json evidence =
      nlohmann::json::object(); // counts, conversions, sources
};

// Validates and resolves everything once; ModelError codes include
// section_not_applicable, section_missing, invalid_bc, load_on_fixed_dof,
// zero_load, rigid_body_motion, thermal_unconstrained,
// thermal_fixture_conflict, port_overlap, inlet_velocity_outward,
// passive_conflict, boundary_in_keep_out, port_in_preserve,
// volume_fraction_infeasible, no_design_elements, disconnected_domain,
// symmetry_domain_mismatch, tie_passive_conflict.
std::shared_ptr<const Problem>
prepare_problem(std::shared_ptr<const TetMesh> mesh, const ProblemSpec &spec);

// Optimizer-side chain helpers: rho = passive(T H expand(x)) with T =
// DesignMap::project_ties, so tied elements receive one physical density
// whatever the filter neighbourhood; gradient is the exact transpose (zero
// passive entries, T^T, H^T, reduce). A custom chain inserting an element-wise
// projection P applies it between T and the passive override.
void design_to_density(const Problem &, const Eigen::VectorXd &x,
                       Eigen::VectorXd &rho);
void density_gradient_to_design(const Problem &, const Eigen::VectorXd &g_rho,
                                Eigen::VectorXd &g_x);
// Refuses "invalid_density": size != E, non-finite or outside [0,1].
void check_density(const Problem &, const Eigen::VectorXd &rho);

// ── Evaluation contract
// ─────────────────────────────────────────────────────────────
enum class FieldLocation { Node, Element };
// Canonical names (SI): displacement(m,node,3) temperature(K,node,1)
// velocity(m/s,node,3) pressure(Pa,node,1) von_mises(Pa,element,1)
// strain_energy_density(J/m3,element,1) heat_flux(W/m2,element,3)
// mode_shape_<k>(-,node,3) buckling_mode_<k>(-,node,3). Multi-case fields
// append "@<case name>".
struct Field {
  std::string name, unit;
  FieldLocation location = FieldLocation::Node;
  int components = 1;
  Eigen::VectorXd values; // location count * components, entity-major
};
// g <= 0 feasible. value = response/limit - 1 for upper limits, 1 -
// response/limit for lower limits; gradient d value / d rho_e (E entries) when
// derivatives were requested.
struct Constraint {
  std::string name, unit;
  double value = 0, response = 0, limit = 0;
  std::string
      aggregation; // "exact" or the precise aggregate, e.g. "p_norm(p=8)"
  Eigen::VectorXd gradient;
};
struct Evaluation {
  double objective = 0; // physical SI value; sense = objective_sense()
  std::string objective_unit;
  Eigen::VectorXd
      objective_gradient; // d objective / d rho_e, E entries (if derivatives)
  std::vector<Constraint> constraints;
  std::vector<Field> fields;
  nlohmann::json evidence =
      nlohmann::json::object(); // residuals, reactions, balance
  bool derivatives = false;
};

class Physics {
public:
  virtual ~Physics() = default;
  virtual PhysicsKind kind() const = 0;
  // Exact formulation label, e.g. "tet4 linear elasticity, weighted compliance,
  // adjoint".
  virtual std::string formulation() const = 0;
  // `density`: physical element densities (E). Gradients are w.r.t. those
  // densities and cover passive elements too (the chain zeroes them). Throws
  // EvaluationCancelled when *cancel becomes true, ModelError on numerical
  // failure; never returns a fake result.
  virtual Evaluation evaluate(const Eigen::VectorXd &density, bool derivatives,
                              const std::atomic<bool> *cancel) = 0;
};

// Achieved design-phase fraction: sum V_e phi_e / V_total over ALL elements,
// phi = rho (solid phase) or 1 - rho (fluid phase), from the geometric density
// only (the property floor min_density never enters volume accounting).
double phase_fraction(const Problem &, const Eigen::VectorXd &rho);
// The same as constraint "<phase>_volume_fraction" (solid_/fluid_), response =
// phase_fraction, limit = settings.volume_fraction, value = response/limit - 1.
Constraint volume_constraint(const Problem &, const Eigen::VectorXd &rho,
                             bool derivatives);

} // namespace topology
} // namespace sim
} // namespace chudware
