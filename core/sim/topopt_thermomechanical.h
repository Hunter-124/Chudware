// Topology optimization: one-way coupled steady thermoelasticity on tet4.
//
// State equations at every density rho (all SI):
//   1. Conduction (ThermalSystem): K_T(rho) theta = q, theta = T - T_ref,
//      T_ref = thermal.reference_temperature_c is the STRAIN-FREE reference
//      (required by prepare_problem). Conductivity k_s s(rho), fixed
//      temperatures and convection exactly as in topopt_thermal.h.
//   2. Element thermal eigenstrain
//        e0_e = alpha * mean_a(theta_{n_a}) * [1, 1, 1, 0, 0, 0]
//      (the element mean of the linear temperature, exact for constant-strain
//      tet4). alpha is an intrinsic material constant and is NOT interpolated.
//   3. Elasticity (ElasticCases): per load case c with its own supports,
//        K(rho) u_c = F_c = f_mech,c + f_th(rho, theta),
//        K = sum_e s(rho_e) V_e B_e^T C B_e,
//        f_th = sum_e s(rho_e) V_e B_e^T C e0_e.
//      Stiffness and thermal load share the single stiffness interpolation s,
//      i.e. element stress s(rho) C (B u - e0) is the one penalized quantity
//      (no second penalization of alpha). Supports are zero displacement.
//
// Objective (compliance, J): weighted mean compliance of the TOTAL load
//   J = sum_c w_c F_c . u_c = sum_c w_c u_c^T K u_c.
// It is the classical thermoelastic compliance: it penalizes thermal
// displacement as well as stress, so a freely expanding body has J > 0 with
// zero stress. The elastic strain energy 0.5 sum s V (Bu-e0)^T C (Bu-e0) is
// reported separately in evidence; it is not the objective.
// Exact gradient (supports fixed at zero):
//   dJ/drho_e = sum_c w_c [ -u_c^T K_e' u_c + 2 u_c^T (d f_th / d rho_e)|theta
//   ]
//             + mu^T ( -K_T,e' theta ),
//   K_T mu = g_theta,  g_theta = sum_c 2 w_c (d f_th / d theta)^T u_c.
//
// Constraints (g <= 0), all with the same chain:
//   max_von_mises[@case]  KS aggregate of eta(rho) vm(C (B u_c - e0)) (Pa),
//                         stress_limit_mpa; the eigenstrain is subtracted.
//   max_displacement[@case] KS aggregate of nodal |u_c| (m),
//                         max_displacement_mm.
//   max_temperature       ThermalSystem KS upper bound (K), max_temperature_c.
// Mechanical constraint gradients: explicit d/drho, one batched mechanical
// adjoint per support set K lambda = dg/du, the explicit stiffness and
// eigenstrain-load terms -lambda^T K' u + lambda^T df_th/drho, and the direct
// plus load eigenstrain sensitivity chained through one thermal adjoint each.
// Case names are appended only when there are several cases.
//
// Fields: temperature (K, node), heat_flux (W/m2, element, 3), thermal_strain
// (-, element, 1: alpha * mean theta), and per case displacement (m, node, 3),
// stress (Pa, element, 6: homogenized s(rho) C (B u - e0), Voigt
// [xx,yy,zz,xy,yz,zx]), von_mises (Pa, element: relaxed eta(rho) vm of
// C (B u - e0)), strain_energy_density (J/m3, element: elastic, eigenstrain
// subtracted), reaction (N, node, 3: support force on the body).
// Evidence: thermal heat-flow balance and residuals, elastic force balance and
// residuals per case, compliance split into mechanical and thermal-load work,
// thermal load resultants (self-equilibrated, ~0), symbolic/numeric
// factorization counts.
//
// Refusals (ModelError): physics_mismatch (physics != thermomechanical),
// objective_not_supported (objective != compliance), section_missing,
// material_missing, invalid_bc (nonzero prescribed support displacement),
// zero_load (alpha = 0 with an unloaded case at construction; a case whose
// total load vanishes at the evaluated density), thermal_unconstrained,
// memory_budget, singular_system, solve_inaccurate. Rigid-body motion is
// refused by prepare_problem; nothing is pinned here.
//
// The evaluator owns one ThermalSystem and one ElasticCases (one symbolic
// analysis per support set and for conduction, numeric refactorization per
// density). Not thread-safe: one evaluation at a time.
#pragma once

#include "sim/topopt_model.h"

#include <memory>

namespace chudware {
namespace sim {
namespace topology {

std::unique_ptr<Physics>
make_thermomechanical_physics(std::shared_ptr<const Problem> problem);

} // namespace topology
} // namespace sim
} // namespace chudware
